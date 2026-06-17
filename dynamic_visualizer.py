import sys
import json
import math
import argparse
import datetime
from pathlib import Path

import networkx as nx

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QGraphicsScene,
    QGraphicsView,
    QGraphicsEllipseItem,
    QGraphicsRectItem,
    QGraphicsLineItem,
    QGraphicsTextItem,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QFrame,
    QScrollArea,
)
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QPen,
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPalette,
    QTransform,
)

C_BG = QColor("#0d1117")
C_PANEL = QColor("#161b22")
C_BORDER = QColor("#30363d")
C_SWITCH = QColor("#1f6feb")
C_SWITCH_T = QColor("#cae8ff")
C_HOST = QColor("#238636")
C_HOST_T = QColor("#acf0a0")
C_LINK_LOW = QColor("#238636")
C_LINK_MED = QColor("#e3b341")
C_LINK_HI = QColor("#da3633")
C_LINK_OFF = QColor("#30363d")
C_HOST_LINK = QColor("#3d444d")
C_TEXT = QColor("#e6edf3")
C_TEXT_DIM = QColor("#8b949e")
C_DOT = QColor("#ffa657")

SCENE_W = 760
SCENE_H = 540
UTIL_DOT_THRESHOLD = 0.02
# How many consecutive polls a previously-seen switch/link may be missing
# from state.json before the visualizer actually removes it. This absorbs
# brief topology blips (e.g. a momentary EventLinkDelete under congestion)
# without ever showing a torn or half-drawn graph.
MISSING_GRACE_POLLS = 3


DEMO_STATE = {
    "timestamp": "demo",
    "nodes": [
        {"id": "s1", "type": "switch"},
        {"id": "s2", "type": "switch"},
        {"id": "s3", "type": "switch"},
        {"id": "s4", "type": "switch"},
        {"id": "h_10.0.0.1", "type": "host", "ip": "10.0.0.1"},
        {"id": "h_10.0.0.2", "type": "host", "ip": "10.0.0.2"},
    ],
    "links": [
        {
            "src": "s1",
            "dst": "s2",
            "src_port": 2,
            "dst_port": 3,
            "rate_mbps": 4.5,
            "util": 0.45,
            "capacity_mbps": 10,
        },
        {
            "src": "s1",
            "dst": "s3",
            "src_port": 3,
            "dst_port": 3,
            "rate_mbps": 1.2,
            "util": 0.12,
            "capacity_mbps": 10,
        },
        {
            "src": "s2",
            "dst": "s4",
            "src_port": 4,
            "dst_port": 2,
            "rate_mbps": 4.5,
            "util": 0.45,
            "capacity_mbps": 10,
        },
        {
            "src": "s3",
            "dst": "s4",
            "src_port": 4,
            "dst_port": 3,
            "rate_mbps": 0.8,
            "util": 0.08,
            "capacity_mbps": 10,
        },
        {
            "src": "h_10.0.0.1",
            "dst": "s1",
            "host_link": True,
            "rate_mbps": 0.0,
            "util": 0.0,
        },
        {
            "src": "h_10.0.0.2",
            "dst": "s4",
            "host_link": True,
            "rate_mbps": 0.0,
            "util": 0.0,
        },
    ],
    "flows": [
        {
            "src_ip": "10.0.0.1",
            "dst_ip": "10.0.0.2",
            "path": ["s1", "s2", "s4"],
            "G": 0.42,
            "rerouted": False,
        },
    ],
    "event": "Demo mode — waiting for state.json",
}


def load_state(path: str) -> tuple:
    p = Path(path)
    if not p.exists():
        return DEMO_STATE, "state.json not found — showing demo data"
    try:
        with p.open("r") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("Root must be a JSON object")
        return data, ""
    except json.JSONDecodeError as exc:
        return DEMO_STATE, f"JSON parse error: {exc}"
    except Exception as exc:
        return DEMO_STATE, f"Read error: {exc}"


def compute_layout(nodes: list, links: list) -> dict:
    """
    Build a NetworkX graph from nodes/links and return
    {node_id: (x_px, y_px)} in scene coordinates.
    Layout is stable — only switch nodes participate in spring_layout;
    host nodes are pinned adjacent to their connected switch.
    """
    # Separate switch nodes from host nodes
    switch_ids = {n["id"] for n in nodes if n.get("type") == "switch"}
    host_ids = {n["id"] for n in nodes if n.get("type") != "switch"}

    # Build switch-only graph for layout
    G_sw = nx.Graph()
    for sid in switch_ids:
        G_sw.add_node(sid)
    for lnk in links:
        s, d = lnk["src"], lnk["dst"]
        if s in switch_ids and d in switch_ids:
            G_sw.add_edge(s, d)

    if len(G_sw.nodes) == 0:
        return {}

    pos_raw = nx.spring_layout(
        G_sw, seed=42, k=2.5 / max(math.sqrt(len(G_sw.nodes)), 1)
    )

    PAD = 80
    xs = [v[0] for v in pos_raw.values()]
    ys = [v[1] for v in pos_raw.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    rx = max(max_x - min_x, 0.001)
    ry = max(max_y - min_y, 0.001)

    result = {}
    for node_id, (nx_x, nx_y) in pos_raw.items():
        sx = PAD + (nx_x - min_x) / rx * (SCENE_W - 2 * PAD)
        sy = PAD + (nx_y - min_y) / ry * (SCENE_H - 2 * PAD)
        result[node_id] = (sx, sy)

    # Build host->switch map
    host_switch = {}
    for lnk in links:
        if lnk.get("host_link"):
            h = lnk["src"] if lnk["src"] in host_ids else lnk["dst"]
            sw = lnk["dst"] if lnk["src"] in host_ids else lnk["src"]
            if sw in result:
                host_switch[h] = sw

    # Pin each host 70px away from its switch at a fixed angle offset
    # so hosts don't overlap each other when multiple hang on the same switch.
    sw_host_count: dict = {}
    for h, sw in host_switch.items():
        sw_host_count[sw] = sw_host_count.get(sw, 0) + 1

    sw_host_idx: dict = {}
    HOST_DIST = 70
    for hid in host_ids:
        sw = host_switch.get(hid)
        if sw is None or sw not in result:
            continue
        sx, sy = result[sw]
        n = sw_host_count.get(sw, 1)
        idx = sw_host_idx.get(sw, 0)
        sw_host_idx[sw] = idx + 1
        # Spread hosts evenly around the switch
        base_angle = math.pi / 4  # 45° — avoids overlap with trunk links
        angle = base_angle + idx * (2 * math.pi / n)
        result[hid] = (
            sx + HOST_DIST * math.cos(angle),
            sy + HOST_DIST * math.sin(angle),
        )

    return result


def _canonical_link_key(src: str, dst: str) -> tuple:
    """Always store / look up a link as (lexicographically smaller, larger)."""
    return (src, dst) if src <= dst else (dst, src)


def util_color(util: float) -> QColor:
    """Green < 0.5, Yellow/orange < 0.8, Red >= 0.8."""
    util = max(0.0, min(float(util), 1.0))
    if util < 0.5:
        t = util / 0.5
        r = int(35 + (227 - 35) * t)
        g = int(134 + (179 - 134) * t)
        b = int(54 + (65 - 54) * t)
        return QColor(r, g, b)
    elif util < 0.8:
        t = (util - 0.5) / 0.3
        r = int(227 + (218 - 227) * t)
        g = int(179 + (54 - 179) * t)
        b = int(65 + (51 - 65) * t)
        return QColor(r, g, b)
    else:
        return C_LINK_HI


class TrafficDot:
    def __init__(self, scene: QGraphicsScene):
        self.dot = QGraphicsEllipseItem(0, 0, 8, 8)
        self.dot.setBrush(QBrush(C_DOT))
        self.dot.setPen(QPen(Qt.PenStyle.NoPen))
        self.dot.setZValue(7)
        self.dot.setVisible(False)
        scene.addItem(self.dot)
        self.t = 0.0
        self.speed = 0.018
        self.x1 = self.y1 = self.x2 = self.y2 = 0.0

    def set_segment(self, x1, y1, x2, y2, speed=0.018):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.speed = speed
        self.dot.setVisible(True)

    def hide(self):
        self.dot.setVisible(False)

    def tick(self):
        self.t = (self.t + self.speed) % 1.0
        x = self.x1 + (self.x2 - self.x1) * self.t
        y = self.y1 + (self.y2 - self.y1) * self.t
        self.dot.setPos(x - 4, y - 4)


class TopologyScene(QGraphicsScene):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackgroundBrush(QBrush(C_BG))
        self.setSceneRect(0, 0, SCENE_W, SCENE_H)

        self._node_items: dict = {}
        # FIX: link items keyed by canonical (small, large) pair so
        # lookups always hit regardless of which direction state.json wrote.
        self._link_items: dict = {}
        self._dots: list = []
        self._last_state: dict = {}
        # FIX: topology signature based only on node set + undirected edge set,
        # not on directed src/dst order — prevents spurious rebuilds when the
        # controller writes the same link with src/dst swapped on different polls.
        self._topo_sig: str = ""
        # Last-known-good node/link sets, used to absorb brief blips: a node
        # or link must be missing for MISSING_GRACE_POLLS consecutive polls
        # before it's actually torn down.
        self._known_node_ids: set = set()
        self._known_link_keys: set = set()
        self._missing_node_count: dict = {}  # node_id -> consecutive misses
        self._missing_link_count: dict = {}  # canonical key -> consecutive misses
        self._node_type_cache: dict = {}  # node_id -> "switch" | "host"
        self._node_ip_cache: dict = {}  # node_id -> ip (for hosts)

        for _ in range(32):
            self._dots.append(TrafficDot(self))

    def _topo_signature(self, nodes: list, links: list) -> str:
        n_ids = sorted(n["id"] for n in nodes)
        # Canonical edge set: sort each edge internally, then sort the list
        edges = sorted(
            f"{min(l['src'], l['dst'])}-{max(l['src'], l['dst'])}" for l in links
        )
        return "|".join(n_ids) + "||" + "|".join(edges)

    def _rebuild_topology(self, nodes: list, links: list):
        for items in self._node_items.values():
            self.removeItem(items["shape"])
            self.removeItem(items["label"])
        self._node_items.clear()

        for items in self._link_items.values():
            self.removeItem(items["line"])
            self.removeItem(items["label"])
        self._link_items.clear()

        positions = compute_layout(nodes, links)

        for n in nodes:
            nid = n["id"]
            is_switch = n.get("type") == "switch"
            pos = positions.get(nid)
            if pos is None:
                continue
            cx, cy = pos
            self._draw_node(nid, cx, cy, is_switch, n.get("ip", ""))

        # Deduplicate links before drawing: same canonical key → draw once
        drawn_keys: set = set()
        for lnk in links:
            src, dst = lnk["src"], lnk["dst"]
            key = _canonical_link_key(src, dst)
            if key in drawn_keys:
                continue
            drawn_keys.add(key)
            if src not in self._node_items or dst not in self._node_items:
                continue
            is_host_link = bool(lnk.get("host_link", False))
            self._draw_link(key, is_host_link=is_host_link)

    def _draw_node(self, nid: str, cx: float, cy: float, is_switch: bool, ip: str = ""):
        if is_switch:
            W, H = 56, 32
            shape = QGraphicsRectItem(cx - W / 2, cy - H / 2, W, H)
            shape.setBrush(QBrush(C_SWITCH))
            shape.setPen(QPen(C_SWITCH.lighter(130), 1.5))
            display_name = nid
            txt_color = C_SWITCH_T
        else:
            W = H = 36
            shape = QGraphicsEllipseItem(cx - W / 2, cy - H / 2, W, H)
            shape.setBrush(QBrush(C_HOST))
            shape.setPen(QPen(C_HOST.lighter(130), 1.5))
            if ip:
                try:
                    octet = int(ip.split(".")[-1])
                    display_name = f"h{octet}"
                except ValueError:
                    display_name = ip
            else:
                display_name = nid
            txt_color = C_HOST_T

        shape.setZValue(3)
        self.addItem(shape)

        label = QGraphicsTextItem(display_name)
        label.setDefaultTextColor(txt_color)
        label.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        bw = label.boundingRect().width()
        bh = label.boundingRect().height()
        label.setPos(cx - bw / 2, cy - bh / 2)
        label.setZValue(4)
        self.addItem(label)

        self._node_items[nid] = {"shape": shape, "label": label, "cx": cx, "cy": cy}

    def _draw_link(self, key: tuple, is_host_link: bool = False):
        """key is always the canonical (small, large) pair."""
        src, dst = key
        n1 = self._node_items[src]
        n2 = self._node_items[dst]

        if is_host_link:
            pen = QPen(C_HOST_LINK, 1.5, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap)
            z = 0
        else:
            pen = QPen(C_LINK_OFF, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            z = 1

        line = QGraphicsLineItem(n1["cx"], n1["cy"], n2["cx"], n2["cy"])
        line.setPen(pen)
        line.setZValue(z)
        self.addItem(line)

        mx = (n1["cx"] + n2["cx"]) / 2
        my = (n1["cy"] + n2["cy"]) / 2
        lbl = QGraphicsTextItem("")
        lbl.setDefaultTextColor(C_TEXT_DIM)
        lbl.setFont(QFont("Courier New", 7))
        lbl.setPos(mx + 4, my - 8)
        lbl.setZValue(5)
        self.addItem(lbl)

        self._link_items[key] = {"line": line, "label": lbl, "host_link": is_host_link}

    def _apply_missing_grace(self, nodes: list, links: list) -> tuple:
        """
        Merge the just-received nodes/links with the last-known-good set so
        that anything missing for fewer than MISSING_GRACE_POLLS consecutive
        polls is kept (using its last-known data) instead of being torn down
        immediately. Returns the (possibly patched) nodes, links to render.
        """
        incoming_node_ids = {n["id"] for n in nodes}
        incoming_link_keys = {_canonical_link_key(l["src"], l["dst"]) for l in links}

        # Refresh type/ip cache from whatever's currently present
        for n in nodes:
            self._node_type_cache[n["id"]] = n.get("type", "switch")
            if n.get("ip"):
                self._node_ip_cache[n["id"]] = n["ip"]

        # ── Nodes ────────────────────────────────────────────────────────────
        for nid in list(self._known_node_ids):
            if nid in incoming_node_ids:
                self._missing_node_count.pop(nid, None)
                continue
            miss = self._missing_node_count.get(nid, 0) + 1
            self._missing_node_count[nid] = miss
            if miss <= MISSING_GRACE_POLLS:
                # Re-inject the node using its cached type so it stays on
                # screen during the grace window.
                node_type = self._node_type_cache.get(nid, "switch")
                patched = {"id": nid, "type": node_type}
                if nid in self._node_ip_cache:
                    patched["ip"] = self._node_ip_cache[nid]
                nodes = nodes + [patched]
                incoming_node_ids.add(nid)
            else:
                self._known_node_ids.discard(nid)

        self._known_node_ids |= incoming_node_ids

        # ── Links ────────────────────────────────────────────────────────────
        # Index incoming links by canonical key, keep last-seen full dict so we
        # can re-inject a faithful copy (including its util/rate) on a miss.
        last_link_by_key = {}
        for l in links:
            last_link_by_key[_canonical_link_key(l["src"], l["dst"])] = l

        for key in list(self._known_link_keys):
            if key in incoming_link_keys:
                self._missing_link_count.pop(key, None)
                continue
            miss = self._missing_link_count.get(key, 0) + 1
            self._missing_link_count[key] = miss
            if miss <= MISSING_GRACE_POLLS:
                cached = self._link_items.get(key)
                if cached is not None:
                    src, dst = key
                    links = links + [
                        {
                            "src": src,
                            "dst": dst,
                            "host_link": cached.get("host_link", False),
                            "rate_mbps": 0.0,
                            "util": 0.0,
                        }
                    ]
                    incoming_link_keys.add(key)
            else:
                self._known_link_keys.discard(key)

        self._known_link_keys |= incoming_link_keys

        return nodes, links

    def update_state(self, state: dict):
        nodes = state.get("nodes", [])
        links = state.get("links", [])
        flows = state.get("flows", [])

        nodes, links = self._apply_missing_grace(nodes, links)

        sig = self._topo_signature(nodes, links)
        if sig != self._topo_sig:
            self._rebuild_topology(nodes, links)
            self._topo_sig = sig

        self._last_state = state
        self._refresh_links(links, flows)

    def _refresh_links(self, links: list, flows: list):
        """
        Update switch-to-switch link colour/thickness and util label.

        FIX: util for each link is accumulated by canonical key so that two
        entries for the same physical link (written as s1↔s2 and s2↔s1 in
        different port-stat cycles) are merged rather than clobbering each
        other. We take the max util seen for the canonical key.
        """
        # Build canonical util map from all link entries in state.json
        util_map: dict = {}  # canonical_key -> {util, rate, src, dst}
        for lnk in links:
            if lnk.get("host_link", False):
                continue
            src, dst = lnk["src"], lnk["dst"]
            key = _canonical_link_key(src, dst)
            util = max(0.0, min(float(lnk.get("util", 0.0)), 1.0))
            rate = float(lnk.get("rate_mbps", 0.0))
            # Keep the entry with the higher util (max of both directions)
            if key not in util_map or util > util_map[key]["util"]:
                util_map[key] = {"util": util, "rate": rate}

        for key, data in util_map.items():
            item = self._link_items.get(key)
            if item is None:
                continue

            util = data["util"]
            rate = data["rate"]
            percent = util * 100.0

            color = util_color(util)
            width = 1.5 + util * 6.0
            pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            item["line"].setPen(pen)

            label = item["label"]
            if rate > 0.01:
                label.setPlainText(f"{percent:5.1f}%  {rate:5.1f}M")
                label.setDefaultTextColor(color.lighter(130))
            else:
                label.setPlainText("")
            label.update()

        # Links not present in util_map (shouldn't happen, but just in case)
        for key, item in self._link_items.items():
            if item.get("host_link"):
                continue
            if key not in util_map:
                item["line"].setPen(QPen(C_LINK_OFF, 2, Qt.PenStyle.SolidLine))
                item["label"].setPlainText("")

        self._update_dots(links, flows)

    def _update_dots(self, links: list, flows: list):
        for dot in self._dots:
            dot.hide()

        # Build util by canonical key (same merge as above)
        util_map: dict = {}
        for lnk in links:
            if lnk.get("host_link", False):
                continue
            src, dst = lnk["src"], lnk["dst"]
            key = _canonical_link_key(src, dst)
            util = float(lnk.get("util", 0.0))
            if util > UTIL_DOT_THRESHOLD:
                if key not in util_map or util > util_map[key]:
                    util_map[key] = util

        dot_idx = 0
        for key, util in util_map.items():
            if dot_idx + 1 >= len(self._dots):
                break
            src, dst = key
            if src not in self._node_items or dst not in self._node_items:
                continue

            n1 = self._node_items[src]
            n2 = self._node_items[dst]
            speed = 0.010 + util * 0.025
            color = util_color(util)

            d0 = self._dots[dot_idx]
            d1 = self._dots[dot_idx + 1]
            dot_idx += 2

            d0.set_segment(n1["cx"], n1["cy"], n2["cx"], n2["cy"], speed)
            d1.set_segment(n1["cx"], n1["cy"], n2["cx"], n2["cy"], speed)
            if d1.t < 0.5:
                d1.t = d0.t + 0.5
            d0.dot.setBrush(QBrush(color))
            d1.dot.setBrush(QBrush(color))

    def tick_animation(self):
        for dot in self._dots:
            if dot.dot.isVisible():
                dot.tick()


def _styled_label(text="—", bold=False, color="#8b949e") -> QLabel:
    lbl = QLabel(text)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(
        f"color: {color}; font-family: 'Courier New'; font-size: 15px; font-weight: {weight};"
    )
    return lbl


class SidePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(250)
        self.setMaximumWidth(480)
        self.setStyleSheet(f"background-color: {C_PANEL.name()};")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        title = QLabel("NETWORK DASHBOARD")
        title.setStyleSheet(
            "color: #58a6ff; font-family: 'Courier New'; font-size: 15px; "
            "font-weight: bold; letter-spacing: 2px; "
            "border-bottom: 1px solid #30363d; padding-bottom: 6px;"
        )
        outer.addWidget(title)

        cong_box = QGroupBox("▲ Top Congested Links")
        cong_box.setStyleSheet(self._gbox_style("#da3633"))
        self._cong_layout = QVBoxLayout(cong_box)
        self._cong_layout.setSpacing(4)
        self._cong_layout.setContentsMargins(8, 12, 8, 8)
        outer.addWidget(cong_box, stretch=1)

        outer.addStretch()

    @staticmethod
    def _gbox_style(accent: str) -> str:
        return f"""
            QGroupBox {{
                border: 1px solid {accent};
                border-radius: 5px;
                margin-top: 8px;
                color: {accent};
                font-family: 'Courier New';
                font-size: 13px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }}
        """

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().hide()
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def refresh(self, state: dict):
        links = state.get("links", [])

        self._clear_layout(self._cong_layout)

        # Merge by canonical key before displaying (same fix as _refresh_links)
        util_map: dict = {}
        for lnk in links:
            if lnk.get("host_link", False):
                continue
            src, dst = lnk["src"], lnk["dst"]
            key = _canonical_link_key(src, dst)
            util = max(0.0, min(float(lnk.get("util", 0.0)), 1.0))
            rate = float(lnk.get("rate_mbps", 0.0))
            if key not in util_map or util > util_map[key]["util"]:
                util_map[key] = {
                    "util": util,
                    "rate": rate,
                    "label": f"{min(src,dst)} ↔ {max(src,dst)}",
                }

        sorted_links = sorted(util_map.values(), key=lambda x: x["util"], reverse=True)

        shown = 0
        for data in sorted_links:
            util = data["util"]
            if util < 0.01:
                continue
            percent = util * 100.0
            rate = data["rate"]
            color = util_color(util).name()
            row = QHBoxLayout()

            edge_lbl = QLabel(data["label"])
            edge_lbl.setStyleSheet(
                "color: #8b949e; font-family: Courier New; font-size: 15px;"
            )
            util_lbl = QLabel(f"{percent:6.1f}%  {rate:6.1f}M")
            util_lbl.setStyleSheet(
                f"color: {color}; font-family: Courier New; "
                f"font-size: 15px; font-weight: bold;"
            )
            row.addWidget(edge_lbl)
            row.addStretch()
            row.addWidget(util_lbl)
            self._cong_layout.addLayout(row)
            shown += 1
            if shown >= 5:
                break

        if shown == 0:
            self._cong_layout.addWidget(_styled_label("No congestion", color="#8b949e"))

        self._cong_layout.addStretch()


class EventLog(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumHeight(130)
        self.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {C_BG.name()};
                color: #8b949e;
                font-family: 'Courier New';
                font-size: 15px;
                border: 1px solid {C_BORDER.name()};
                border-radius: 4px;
                padding: 4px;
            }}
        """)
        self._last_event: str = ""

    def append_event(self, message: str, force: bool = False):
        if message == self._last_event and not force:
            return
        self._last_event = message
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.appendPlainText(f"[{ts}]  {message}")
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def log(self, message: str):
        self.append_event(message, force=True)


class MainWindow(QMainWindow):
    def __init__(self, state_file: str = "state.json"):
        super().__init__()
        self._state_file = state_file
        self._poll_count = 0

        self._setup_window()
        self._build_ui()
        self._setup_timers()

        self._poll_state()
        self._event_log.log("Dynamic visualizer started — polling " + state_file)

    def _setup_window(self):
        self.setWindowTitle("SDN Active Inference — Dynamic Monitor")
        self.resize(1100, 740)
        self.setMinimumSize(800, 580)

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, C_BG)
        pal.setColor(QPalette.ColorRole.WindowText, C_TEXT)
        pal.setColor(QPalette.ColorRole.Base, C_PANEL)
        pal.setColor(QPalette.ColorRole.Text, C_TEXT)
        self.setPalette(pal)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        header = QLabel("  ◈  SDN ACTIVE INFERENCE — DYNAMIC TOPOLOGY MONITOR")
        header.setStyleSheet(
            "color: #58a6ff; font-family: 'Courier New'; font-size: 13px;"
            "font-weight: bold; letter-spacing: 2px;"
            "background: #161b22; border-bottom: 1px solid #30363d;"
            "padding: 6px 0;"
        )
        root.addWidget(header)

        mid = QSplitter(Qt.Orientation.Horizontal)
        mid.setStyleSheet("QSplitter::handle { background: #30363d; width: 2px; }")

        self._scene = TopologyScene(self)
        self._view = QGraphicsView(self._scene)
        self._view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._view.setBackgroundBrush(QBrush(C_BG))
        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setMinimumWidth(520)
        mid.addWidget(self._view)

        self._side = SidePanel()
        mid.addWidget(self._side)
        mid.setStretchFactor(0, 3)
        mid.setStretchFactor(1, 1)
        mid.setSizes([820, 340])
        root.addWidget(mid, stretch=1)

        log_label = QLabel("EVENT LOG")
        log_label.setStyleSheet(
            "color: #8b949e; font-family: 'Courier New'; font-size: 10px;"
            "letter-spacing: 2px; padding: 2px 0;"
        )
        root.addWidget(log_label)

        self._event_log = EventLog()
        root.addWidget(self._event_log)

        self.statusBar().setStyleSheet(
            "color: #8b949e; font-family: 'Courier New'; font-size: 12px;"
            "background: #161b22; border-top: 1px solid #30363d;"
        )
        self.statusBar().showMessage("Initialising…")

    def _setup_timers(self):
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_state)
        self._poll_timer.start(1000)

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._scene.tick_animation)
        self._anim_timer.start(50)

    def _poll_state(self):
        state, err = load_state(self._state_file)
        self._poll_count += 1

        self._scene.update_state(state)
        self._side.refresh(state)

        event_text = str(state.get("event", ""))
        if event_text:
            self._event_log.append_event(event_text)
        if err:
            self._event_log.append_event(f"⚠  {err}")

        ts = state.get("timestamp", "—")
        n_sw = sum(1 for n in state.get("nodes", []) if n.get("type") == "switch")
        n_host = sum(1 for n in state.get("nodes", []) if n.get("type") == "host")
        self.statusBar().showMessage(
            f"  Last update: {ts}   |   Switches: {n_sw}   |   "
            f"Hosts: {n_host}   |   Polls: {self._poll_count}"
        )

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._view.fitInView(
            self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio
        )


def main():
    parser = argparse.ArgumentParser(
        description="Dynamic SDN Active Inference Visualizer"
    )
    parser.add_argument(
        "--state",
        default="state.json",
        help="Path to state.json written by the Ryu controller (default: state.json)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    dark = app.palette()
    dark.setColor(QPalette.ColorRole.Window, QColor("#0d1117"))
    dark.setColor(QPalette.ColorRole.WindowText, QColor("#e6edf3"))
    dark.setColor(QPalette.ColorRole.Base, QColor("#161b22"))
    dark.setColor(QPalette.ColorRole.AlternateBase, QColor("#0d1117"))
    dark.setColor(QPalette.ColorRole.ToolTipBase, QColor("#161b22"))
    dark.setColor(QPalette.ColorRole.ToolTipText, QColor("#e6edf3"))
    dark.setColor(QPalette.ColorRole.Text, QColor("#e6edf3"))
    dark.setColor(QPalette.ColorRole.Button, QColor("#21262d"))
    dark.setColor(QPalette.ColorRole.ButtonText, QColor("#e6edf3"))
    dark.setColor(QPalette.ColorRole.Highlight, QColor("#1f6feb"))
    dark.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(dark)

    win = MainWindow(state_file=args.state)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
