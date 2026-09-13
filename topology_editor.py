"""
topology_editor.py — Visual topology editor (task doc §4).

Split into two layers on purpose:

  TopologyEditorModel   — plain-Python graph model (nodes/links + to/from
                           spec dict + validation). No Qt imports at all,
                           so it's testable without PyQt6 installed and
                           without a display.

  TopologyEditorScene /
  TopologyEditorWidget — the QGraphicsScene-based visual layer (reusing the
                           dark theme / canvas approach from
                           dynamic_visualizer.py) that lets the user
                           manipulate a TopologyEditorModel with the mouse,
                           then Save/Load/Apply it.

Functional spec covered (task doc §4):
  - add/delete switch or host node (deleting a node deletes incident links)
  - draw a link between two selected nodes (host<->switch or switch<->switch)
  - edit a link's bandwidth / delay / (switch-switch only) queue size
  - edit a host's IP, constrained to the configured Mininet subnet
  - save/load named topologies to/from disk as JSON; default preset
  - "Apply" validates the graph then emits apply_requested(spec)
"""

from __future__ import annotations

import json
import math
import re
from typing import Optional

from sdn.topology_spec import (
    DEFAULT_SUBNET_CIDR,
    DEFAULT_SUBNET_PREFIX,
    TopologySpecError,
    default_spec,
    validate_spec,
)

SWITCH_ID_RE = re.compile(r"^s(\d+)$")
HOST_ID_RE = re.compile(r"^h(\d+)$")


# =============================================================================
#  Pure-Python model — no Qt dependency
# =============================================================================


class TopologyEditorModel:
    """
    In-memory topology graph the editor mutates. `nodes[id] = {"kind":
    "switch"|"host", "x": float, "y": float, "ip": str|None}`.
    `links[(a, b)]` (canonical a<b string sort) `= {"bw": float, "delay":
    str, "max_queue_size": int|None}`.
    """

    def __init__(self):
        self.nodes: dict = {}
        self.links: dict = {}
        self.controller = {"ip": "127.0.0.1", "port": 6633}

    # ── id allocation ────────────────────────────────────────────────────

    def _next_id(self, prefix: str) -> str:
        pat = SWITCH_ID_RE if prefix == "s" else HOST_ID_RE
        used = {
            int(pat.match(nid).group(1))
            for nid in self.nodes
            if pat.match(nid)
        }
        n = 1
        while n in used:
            n += 1
        return f"{prefix}{n}"

    # ── node CRUD ────────────────────────────────────────────────────────

    def add_switch(self, x: float, y: float) -> str:
        nid = self._next_id("s")
        self.nodes[nid] = {"kind": "switch", "x": x, "y": y, "ip": None}
        return nid

    def add_host(self, x: float, y: float, ip: Optional[str] = None) -> str:
        nid = self._next_id("h")
        if ip is None:
            ip = self._next_free_ip()
        self.nodes[nid] = {"kind": "host", "x": x, "y": y, "ip": ip}
        return nid

    def _next_free_ip(self) -> str:
        used = set()
        for n in self.nodes.values():
            if n.get("ip"):
                try:
                    used.add(int(n["ip"].split("/")[0].split(".")[-1]))
                except ValueError:
                    pass
        for octet in range(1, 255):
            if octet not in used:
                return f"{DEFAULT_SUBNET_PREFIX}{octet}/24"
        raise TopologySpecError("subnet exhausted (254 host limit)")

    def remove_node(self, nid: str) -> None:
        self.nodes.pop(nid, None)
        for key in [k for k in self.links if nid in k]:
            del self.links[key]

    def set_host_ip(self, nid: str, ip: str) -> None:
        node = self.nodes.get(nid)
        if node is None or node["kind"] != "host":
            raise TopologySpecError(f"{nid!r} is not a host")
        node["ip"] = ip

    def move_node(self, nid: str, x: float, y: float) -> None:
        if nid in self.nodes:
            self.nodes[nid]["x"] = x
            self.nodes[nid]["y"] = y

    # ── link CRUD ────────────────────────────────────────────────────────

    @staticmethod
    def _key(a: str, b: str) -> tuple:
        return (a, b) if a <= b else (b, a)

    def can_link(self, a: str, b: str) -> Optional[str]:
        """Return an error string if a<->b would be invalid, else None."""
        if a == b:
            return "a node cannot link to itself"
        if a not in self.nodes or b not in self.nodes:
            return "unknown node"
        if self.nodes[a]["kind"] == "host" and self.nodes[b]["kind"] == "host":
            return "hosts must connect through a switch, not to each other"
        if self._key(a, b) in self.links:
            return "a link already exists between these nodes"
        return None

    def add_link(
        self,
        a: str,
        b: str,
        bw: float = 10.0,
        delay: str = "10ms",
        max_queue_size: Optional[int] = 50,
    ) -> None:
        err = self.can_link(a, b)
        if err:
            raise TopologySpecError(err)
        is_switch_switch = (
            self.nodes[a]["kind"] == "switch" and self.nodes[b]["kind"] == "switch"
        )
        link = {"bw": bw, "delay": delay}
        if is_switch_switch:
            link["max_queue_size"] = max_queue_size
        self.links[self._key(a, b)] = link

    def remove_link(self, a: str, b: str) -> None:
        self.links.pop(self._key(a, b), None)

    def update_link(self, a: str, b: str, **props) -> None:
        key = self._key(a, b)
        if key not in self.links:
            raise TopologySpecError("no such link")
        self.links[key].update({k: v for k, v in props.items() if v is not None})

    # ── spec <-> model ───────────────────────────────────────────────────

    def to_spec(self) -> dict:
        switches = [
            {"id": nid} for nid, n in self.nodes.items() if n["kind"] == "switch"
        ]
        hosts = [
            {"id": nid, "ip": n["ip"]}
            for nid, n in self.nodes.items()
            if n["kind"] == "host"
        ]
        links = []
        for (a, b), props in self.links.items():
            link = {"src": a, "dst": b, "bw": props["bw"], "delay": props.get("delay", "1ms")}
            if props.get("max_queue_size") is not None:
                link["max_queue_size"] = props["max_queue_size"]
            links.append(link)
        return {
            "controller": dict(self.controller),
            "switches": switches,
            "hosts": hosts,
            "links": links,
        }

    @classmethod
    def from_spec(cls, spec: dict) -> "TopologyEditorModel":
        model = cls()
        model.controller = dict(spec.get("controller", {"ip": "127.0.0.1", "port": 6633}))

        switch_ids = [s["id"] for s in spec.get("switches", [])]
        host_ids = [h["id"] for h in spec.get("hosts", [])]
        n_sw = max(len(switch_ids), 1)
        for i, sid in enumerate(switch_ids):
            angle = 2 * math.pi * i / n_sw
            model.nodes[sid] = {
                "kind": "switch",
                "x": 380 + 160 * math.cos(angle),
                "y": 260 + 160 * math.sin(angle),
                "ip": None,
            }
        host_by_switch: dict = {}
        for link in spec.get("links", []):
            if link["src"] in host_ids and link["dst"] in switch_ids:
                host_by_switch.setdefault(link["dst"], []).append(link["src"])
            elif link["dst"] in host_ids and link["src"] in switch_ids:
                host_by_switch.setdefault(link["src"], []).append(link["dst"])

        ip_by_id = {h["id"]: h["ip"] for h in spec.get("hosts", [])}
        for sid, hids in host_by_switch.items():
            base = model.nodes.get(sid, {"x": 380, "y": 260})
            for j, hid in enumerate(hids):
                angle = math.pi / 4 + j * (2 * math.pi / max(len(hids), 1))
                model.nodes[hid] = {
                    "kind": "host",
                    "x": base["x"] + 90 * math.cos(angle),
                    "y": base["y"] + 90 * math.sin(angle),
                    "ip": ip_by_id.get(hid),
                }
        # Any host not reachable from the loop above (shouldn't happen for a
        # valid spec, but keep the round-trip total) still gets placed.
        for hid in host_ids:
            if hid not in model.nodes:
                model.nodes[hid] = {"kind": "host", "x": 40, "y": 40, "ip": ip_by_id.get(hid)}

        for link in spec.get("links", []):
            key = cls._key(link["src"], link["dst"])
            model.links[key] = {
                "bw": link["bw"],
                "delay": link.get("delay", "1ms"),
                "max_queue_size": link.get("max_queue_size"),
            }
        return model

    def validate(self) -> list:
        """Raises TopologySpecError, or returns a list of warning strings."""
        return validate_spec(self.to_spec())

    def save(self, path: str) -> list:
        spec = self.to_spec()
        warnings = validate_spec(spec)
        tmp = path + ".tmp"
        import os

        with open(tmp, "w") as fh:
            json.dump(spec, fh, indent=2)
        os.replace(tmp, path)
        return warnings

    @classmethod
    def load(cls, path: str) -> "TopologyEditorModel":
        with open(path, "r") as fh:
            spec = json.load(fh)
        validate_spec(spec)
        return cls.from_spec(spec)

    @classmethod
    def default(cls) -> "TopologyEditorModel":
        return cls.from_spec(default_spec())


# =============================================================================
#  Qt visual layer
# =============================================================================

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

# Reuse the dark theme palette from the visualizer for a consistent look
# (task doc §4: "reuse the existing QGraphicsScene canvas approach").
C_BG = QColor("#12161c")
C_SWITCH = QColor("#2b6cb0")
C_HOST = QColor("#2f855a")
C_LINK = QColor("#4a5568")
C_LINK_SEL = QColor("#e2b93d")
C_TEXT = QColor("#e6e6e6")
C_TEXT_DIM = QColor("#9aa5b1")

NODE_SW_SIZE = (56, 32)
NODE_HOST_SIZE = (36, 36)


class _NodeItem:
    def __init__(self, scene: "TopologyEditorScene", nid: str, kind: str, x: float, y: float):
        self.nid = nid
        self.kind = kind
        if kind == "switch":
            w, h = NODE_SW_SIZE
            self.shape = QGraphicsRectItem(x - w / 2, y - h / 2, w, h)
            self.shape.setBrush(QBrush(C_SWITCH))
        else:
            w, h = NODE_HOST_SIZE
            self.shape = QGraphicsEllipseItem(x - w / 2, y - h / 2, w, h)
            self.shape.setBrush(QBrush(C_HOST))
        self.shape.setPen(QPen(QColor("#e6e6e6"), 1.5))
        self.shape.setZValue(3)
        self.shape.setFlag(self.shape.GraphicsItemFlag.ItemIsMovable, True)
        self.shape.setFlag(self.shape.GraphicsItemFlag.ItemIsSelectable, True)
        scene.addItem(self.shape)

        self.label = QGraphicsTextItem(nid)
        self.label.setDefaultTextColor(C_TEXT)
        self.label.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self.label.setZValue(4)
        scene.addItem(self.label)
        self.set_pos(x, y)

    def set_pos(self, x: float, y: float) -> None:
        if self.kind == "switch":
            w, h = NODE_SW_SIZE
        else:
            w, h = NODE_HOST_SIZE
        self.shape.setPos(0, 0)
        self.shape.setRect(x - w / 2, y - h / 2, w, h)
        bw = self.label.boundingRect().width()
        bh = self.label.boundingRect().height()
        self.label.setPos(x - bw / 2, y - bh / 2)

    def center(self) -> QPointF:
        r = self.shape.rect()
        return r.center()

    def remove(self, scene: QGraphicsScene) -> None:
        scene.removeItem(self.shape)
        scene.removeItem(self.label)


class _LinkItem:
    def __init__(self, scene: QGraphicsScene, x1, y1, x2, y2):
        self.line = QGraphicsLineItem(x1, y1, x2, y2)
        self.line.setPen(QPen(C_LINK, 2.5))
        self.line.setZValue(1)
        self.line.setFlag(self.line.GraphicsItemFlag.ItemIsSelectable, True)
        scene.addItem(self.line)
        self.label = QGraphicsTextItem("")
        self.label.setDefaultTextColor(C_TEXT_DIM)
        self.label.setFont(QFont("Courier New", 7))
        self.label.setZValue(2)
        scene.addItem(self.label)

    def set_geometry(self, x1, y1, x2, y2, text: str) -> None:
        self.line.setLine(x1, y1, x2, y2)
        self.label.setPlainText(text)
        self.label.setPos((x1 + x2) / 2 + 4, (y1 + y2) / 2 - 8)

    def set_selected(self, selected: bool) -> None:
        self.line.setPen(QPen(C_LINK_SEL if selected else C_LINK, 2.5))

    def remove(self, scene: QGraphicsScene) -> None:
        scene.removeItem(self.line)
        scene.removeItem(self.label)


class LinkPropertiesDialog(QDialog):
    def __init__(self, bw: float, delay: str, max_queue_size, is_switch_switch: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Link properties")
        form = QFormLayout(self)

        self.bw_spin = QDoubleSpinBox()
        self.bw_spin.setRange(0.1, 10000.0)
        self.bw_spin.setSuffix(" Mbps")
        self.bw_spin.setValue(bw)
        form.addRow("Bandwidth", self.bw_spin)

        self.delay_edit = QLineEdit(delay)
        self.delay_edit.setPlaceholderText("e.g. 10ms")
        form.addRow("Delay", self.delay_edit)

        self.queue_spin = None
        if is_switch_switch:
            self.queue_spin = QSpinBox()
            self.queue_spin.setRange(1, 10000)
            self.queue_spin.setValue(max_queue_size or 50)
            form.addRow("Max queue size", self.queue_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> dict:
        out = {"bw": self.bw_spin.value(), "delay": self.delay_edit.text().strip() or "1ms"}
        if self.queue_spin is not None:
            out["max_queue_size"] = self.queue_spin.value()
        return out


class HostPropertiesDialog(QDialog):
    def __init__(self, ip: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Host properties")
        form = QFormLayout(self)
        self.ip_edit = QLineEdit(ip)
        self.ip_edit.setPlaceholderText(f"e.g. {DEFAULT_SUBNET_PREFIX}7/24")
        form.addRow(f"IP (subnet {DEFAULT_SUBNET_CIDR})", self.ip_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def ip(self) -> str:
        return self.ip_edit.text().strip()


class TopologyEditorScene(QGraphicsScene):
    """Visual layer bound to a TopologyEditorModel. Modes: 'select', 'add_switch',
    'add_host', 'link', 'delete'."""

    model_changed = pyqtSignal()
    status_message = pyqtSignal(str)

    def __init__(self, model: TopologyEditorModel, parent=None):
        super().__init__(parent)
        self.setBackgroundBrush(QBrush(C_BG))
        self.setSceneRect(0, 0, 760, 520)
        self.model = model
        self.mode = "select"
        self._link_pending_src: Optional[str] = None
        self._node_items: dict = {}
        self._link_items: dict = {}
        self._selected_link_key: Optional[tuple] = None
        self.rebuild()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._link_pending_src = None

    def rebuild(self) -> None:
        for item in self._node_items.values():
            item.remove(self)
        for item in self._link_items.values():
            item.remove(self)
        self._node_items.clear()
        self._link_items.clear()

        for nid, n in self.model.nodes.items():
            self._node_items[nid] = _NodeItem(self, nid, n["kind"], n["x"], n["y"])
        for (a, b), props in self.model.links.items():
            self._add_link_visual(a, b, props)
        self.model_changed.emit()

    def _link_label(self, props: dict) -> str:
        q = f" q{props['max_queue_size']}" if props.get("max_queue_size") else ""
        return f"{props['bw']:g}Mbps {props.get('delay', '')}{q}"

    def _add_link_visual(self, a: str, b: str, props: dict) -> None:
        na, nb = self._node_items[a], self._node_items[b]
        ca, cb = na.center(), nb.center()
        item = _LinkItem(self, ca.x(), ca.y(), cb.x(), cb.y())
        item.set_geometry(ca.x(), ca.y(), cb.x(), cb.y(), self._link_label(props))
        self._link_items[TopologyEditorModel._key(a, b)] = item

    def _refresh_links_for_node(self, nid: str) -> None:
        for key, item in self._link_items.items():
            if nid in key:
                a, b = key
                ca, cb = self._node_items[a].center(), self._node_items[b].center()
                item.set_geometry(ca.x(), ca.y(), cb.x(), cb.y(), self._link_label(self.model.links[key]))

    # ── mouse handling ──────────────────────────────────────────────────

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        pos = event.scenePos()
        clicked_nid = self._node_at(pos)

        if self.mode == "add_switch" and clicked_nid is None:
            nid = self.model.add_switch(pos.x(), pos.y())
            self._node_items[nid] = _NodeItem(self, nid, "switch", pos.x(), pos.y())
            self.status_message.emit(f"Added switch {nid}")
            self.model_changed.emit()
            return

        if self.mode == "add_host" and clicked_nid is None:
            nid = self.model.add_host(pos.x(), pos.y())
            self._node_items[nid] = _NodeItem(self, nid, "host", pos.x(), pos.y())
            self.status_message.emit(f"Added host {nid} ({self.model.nodes[nid]['ip']})")
            self.model_changed.emit()
            return

        if self.mode == "link" and clicked_nid is not None:
            if self._link_pending_src is None:
                self._link_pending_src = clicked_nid
                self.status_message.emit(f"Linking from {clicked_nid} — click a second node")
            else:
                src = self._link_pending_src
                self._link_pending_src = None
                err = self.model.can_link(src, clicked_nid)
                if err:
                    self.status_message.emit(f"Can't link: {err}")
                else:
                    is_ss = (
                        self.model.nodes[src]["kind"] == "switch"
                        and self.model.nodes[clicked_nid]["kind"] == "switch"
                    )
                    self.model.add_link(
                        src,
                        clicked_nid,
                        bw=10.0 if is_ss else 50.0,
                        delay="10ms" if is_ss else "1ms",
                        max_queue_size=50 if is_ss else None,
                    )
                    self._add_link_visual(
                        src, clicked_nid, self.model.links[self.model._key(src, clicked_nid)]
                    )
                    self.status_message.emit(f"Linked {src} <-> {clicked_nid}")
                    self.model_changed.emit()
            return

        if self.mode == "delete":
            if clicked_nid is not None:
                self.model.remove_node(clicked_nid)
                self._node_items.pop(clicked_nid).remove(self)
                for key in [k for k in list(self._link_items) if clicked_nid in k]:
                    self._link_items.pop(key).remove(self)
                self.status_message.emit(f"Deleted {clicked_nid}")
                self.model_changed.emit()
                return
            link_key = self._link_at(pos)
            if link_key is not None:
                a, b = link_key
                self.model.remove_link(a, b)
                self._link_items.pop(link_key).remove(self)
                self.status_message.emit(f"Deleted link {a}<->{b}")
                self.model_changed.emit()
                return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        pos = event.scenePos()
        clicked_nid = self._node_at(pos)
        if clicked_nid is not None and self.model.nodes[clicked_nid]["kind"] == "host":
            dlg = HostPropertiesDialog(self.model.nodes[clicked_nid]["ip"] or "")
            if dlg.exec() == QDialog.DialogCode.Accepted:
                try:
                    self.model.set_host_ip(clicked_nid, dlg.ip())
                    self.status_message.emit(f"{clicked_nid} ip set to {dlg.ip()}")
                    self.model_changed.emit()
                except TopologySpecError as exc:
                    self.status_message.emit(f"Error: {exc}")
            return

        link_key = self._link_at(pos)
        if link_key is not None:
            a, b = link_key
            props = self.model.links[link_key]
            is_ss = self.model.nodes[a]["kind"] == "switch" and self.model.nodes[b]["kind"] == "switch"
            dlg = LinkPropertiesDialog(
                props["bw"], props.get("delay", "1ms"), props.get("max_queue_size"), is_ss
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.model.update_link(a, b, **dlg.values())
                self._link_items[link_key].set_geometry(
                    *self._link_geom(a, b), self._link_label(self.model.links[link_key])
                )
                self.status_message.emit(f"Updated link {a}<->{b}")
                self.model_changed.emit()
            return

        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        super().mouseMoveEvent(event)
        if self.mode == "select":
            # After Qt moves selected shapes via the built-in drag handling,
            # sync the model + dependent link geometry to match.
            for nid, item in self._node_items.items():
                pos = item.shape.sceneBoundingRect().center()
                if abs(pos.x() - self.model.nodes[nid]["x"]) > 0.5 or abs(
                    pos.y() - self.model.nodes[nid]["y"]
                ) > 0.5:
                    self.model.move_node(nid, pos.x(), pos.y())
                    item.label.setPos(
                        pos.x() - item.label.boundingRect().width() / 2,
                        pos.y() - item.label.boundingRect().height() / 2,
                    )
                    self._refresh_links_for_node(nid)

    def _link_geom(self, a: str, b: str) -> tuple:
        ca, cb = self._node_items[a].center(), self._node_items[b].center()
        return ca.x(), ca.y(), cb.x(), cb.y()

    def _node_at(self, pos: QPointF) -> Optional[str]:
        for nid, item in self._node_items.items():
            if item.shape.sceneBoundingRect().contains(pos):
                return nid
        return None

    def _link_at(self, pos: QPointF, tol: float = 6.0) -> Optional[tuple]:
        for key, item in self._link_items.items():
            line = item.line.line()
            if _point_near_segment(pos, line.p1(), line.p2(), tol):
                return key
        return None


def _point_near_segment(p: QPointF, a: QPointF, b: QPointF, tol: float) -> bool:
    ax, ay, bx, by, px, py = a.x(), a.y(), b.x(), b.y(), p.x(), p.y()
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay) <= tol
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy) <= tol


class TopologyEditorWidget(QWidget):
    """Dockable panel: toolbar + canvas + Save/Load/Apply."""

    apply_requested = pyqtSignal(str)  # emits the spec file path just saved

    def __init__(self, spec_path: str, parent=None):
        super().__init__(parent)
        self.spec_path = spec_path
        self.model = TopologyEditorModel.default()
        self.scene = TopologyEditorScene(self.model)
        self.scene.status_message.connect(self._set_status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        toolbar = QToolBar()
        self.mode_buttons: dict = {}
        for label, mode in [
            ("Select/Move", "select"),
            ("+ Switch", "add_switch"),
            ("+ Host", "add_host"),
            ("Link", "link"),
            ("Delete", "delete"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, m=mode: self._set_mode(m))
            toolbar.addWidget(btn)
            self.mode_buttons[mode] = btn
        self.mode_buttons["select"].setChecked(True)

        toolbar.addSeparator()
        save_btn = QPushButton("Save As...")
        save_btn.clicked.connect(self._save_as)
        load_btn = QPushButton("Load...")
        load_btn.clicked.connect(self._load)
        reset_btn = QPushButton("Reset to default")
        reset_btn.clicked.connect(self._reset_default)
        apply_btn = QPushButton("Apply")
        apply_btn.setStyleSheet("font-weight: bold;")
        apply_btn.clicked.connect(self._apply)
        for b in (save_btn, load_btn, reset_btn, apply_btn):
            toolbar.addWidget(b)

        layout.addWidget(toolbar)

        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(self.view.renderHints().Antialiasing)
        layout.addWidget(self.view)

        self.status_label = QLabel("Ready.")
        self.status_label.setStyleSheet("color: #9aa5b1;")
        layout.addWidget(self.status_label)

    def _set_mode(self, mode: str) -> None:
        for m, btn in self.mode_buttons.items():
            btn.setChecked(m == mode)
        self.scene.set_mode(mode)
        self._set_status(f"Mode: {mode}")

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save topology", self.spec_path, "Topology JSON (*.json)"
        )
        if not path:
            return
        try:
            warnings = self.model.save(path)
            self._set_status(f"Saved to {path}" + (f" ({'; '.join(warnings)})" if warnings else ""))
        except TopologySpecError as exc:
            QMessageBox.warning(self, "Invalid topology", str(exc))

    def _load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load topology", "", "Topology JSON (*.json)"
        )
        if not path:
            return
        try:
            self.model = TopologyEditorModel.load(path)
            self.scene.model = self.model
            self.scene.rebuild()
            self._set_status(f"Loaded {path}")
        except (TopologySpecError, OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Invalid topology", str(exc))

    def _reset_default(self) -> None:
        self.model = TopologyEditorModel.default()
        self.scene.model = self.model
        self.scene.rebuild()
        self._set_status("Reset to default topology")

    def _apply(self) -> None:
        try:
            warnings = self.model.validate()
        except TopologySpecError as exc:
            QMessageBox.warning(self, "Cannot apply", str(exc))
            return
        if warnings:
            reply = QMessageBox.question(
                self,
                "Topology warnings",
                "\n".join(warnings) + "\n\nApply anyway?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        try:
            self.model.save(self.spec_path)
        except TopologySpecError as exc:
            QMessageBox.warning(self, "Cannot apply", str(exc))
            return
        self._set_status(f"Applying topology from {self.spec_path}...")
        self.apply_requested.emit(self.spec_path)
