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


DEMO_STATE = {
    "timestamp": "demo",
    "nodes": [
        {"id": "s1", "type": "switch"},
        {"id": "s2", "type": "switch"},
        {"id": "s3", "type": "switch"},
        {"id": "s4", "type": "switch"},
        {"id": "h_10.0.0.1", "type": "host", "ip": "10.0.0.1"},
        {"id": "h_10.0.0.2", "type": "host", "ip": "10.0.0.2"},
        {"id": "h_10.0.0.3", "type": "host", "ip": "10.0.0.3"},
        {"id": "h_10.0.0.4", "type": "host", "ip": "10.0.0.4"},
        {"id": "h_10.0.0.5", "type": "host", "ip": "10.0.0.5"},
        {"id": "h_10.0.0.6", "type": "host", "ip": "10.0.0.6"},
    ],
    "links": [
        # Switch-to-switch links (with utilisation)
        {
            "src": "s1",
            "dst": "s2",
            "src_port": 2,
            "dst_port": 1,
            "rate_mbps": 4.5,
            "util": 0.45,
            "capacity_mbps": 10,
        },
        {
            "src": "s1",
            "dst": "s3",
            "src_port": 3,
            "dst_port": 1,
            "rate_mbps": 1.2,
            "util": 0.12,
            "capacity_mbps": 10,
        },
        {
            "src": "s2",
            "dst": "s4",
            "src_port": 3,
            "dst_port": 2,
            "rate_mbps": 4.5,
            "util": 0.45,
            "capacity_mbps": 10,
        },
        {
            "src": "s3",
            "dst": "s4",
            "src_port": 3,
            "dst_port": 2,
            "rate_mbps": 0.8,
            "util": 0.08,
            "capacity_mbps": 10,
        },
        # Host-to-switch links (plain topology edges, no util)
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
        {
            "src": "h_10.0.0.3",
            "dst": "s2",
            "host_link": True,
            "rate_mbps": 0.0,
            "util": 0.0,
        },
        {
            "src": "h_10.0.0.4",
            "dst": "s2",
            "host_link": True,
            "rate_mbps": 0.0,
            "util": 0.0,
        },
        {
            "src": "h_10.0.0.5",
            "dst": "s3",
            "host_link": True,
            "rate_mbps": 0.0,
            "util": 0.0,
        },
        {
            "src": "h_10.0.0.6",
            "dst": "s3",
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

    Host nodes are included so spring_layout places them adjacent to their
    connected switch, giving a natural topology appearance.
    """
    G = nx.Graph()
    for n in nodes:
        G.add_node(n["id"])
    for lnk in links:
        src, dst = lnk["src"], lnk["dst"]
        if G.has_node(src) and G.has_node(dst):
            G.add_edge(src, dst)

    if len(G.nodes) == 0:
        return {}

    pos_raw = nx.spring_layout(G, seed=42, k=2.5 / max(math.sqrt(len(G.nodes)), 1))

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

    return result


def util_color(util: float) -> QColor:
    """Green < 0.5, Yellow/orange < 0.8, Red >= 0.8."""
    util = max(0.0, min(float(util), 1.0))  # clamp so colour never wraps
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
    _topo_sig: str = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackgroundBrush(QBrush(C_BG))
        self.setSceneRect(0, 0, SCENE_W, SCENE_H)

        self._node_items: dict = {}
        self._link_items: dict = {}
        self._dots: list = []
        self._last_state: dict = {}

        for _ in range(32):
            self._dots.append(TrafficDot(self))

    def _topo_signature(self, nodes: list, links: list) -> str:
        n_ids = sorted(n["id"] for n in nodes)
        l_ids = sorted(f"{l['src']}-{l['dst']}" for l in links)
        return "|".join(n_ids) + "||" + "|".join(l_ids)

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

        for lnk in links:
            src, dst = lnk["src"], lnk["dst"]
            if src not in self._node_items or dst not in self._node_items:
                continue
            is_host_link = bool(lnk.get("host_link", False))
            self._draw_link(src, dst, is_host_link=is_host_link)

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

    def _draw_link(self, src: str, dst: str, is_host_link: bool = False):
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

        key = (src, dst)
        self._link_items[key] = {"line": line, "label": lbl, "host_link": is_host_link}

    def update_state(self, state: dict):
        nodes = state.get("nodes", [])
        links = state.get("links", [])
        flows = state.get("flows", [])

        sig = self._topo_signature(nodes, links)
        if sig != self._topo_sig:
            self._rebuild_topology(nodes, links)
            self._topo_sig = sig

        self._last_state = state
        self._refresh_links(links, flows)

    def _refresh_links(self, links: list, flows: list):
        """Update switch-to-switch link thickness/colour and util label.
        Host-switch links are skipped — they have no util data."""
        flow_edges = set()
        for flow in flows:
            path = flow.get("path", [])
            for i in range(len(path) - 1):
                flow_edges.add((path[i], path[i + 1]))
                flow_edges.add((path[i + 1], path[i]))

        for lnk in links:
            if lnk.get("host_link", False):
                continue

            src, dst = lnk["src"], lnk["dst"]
            util = max(0.0, min(float(lnk.get("util", 0.0)), 1.0))
            rate = float(lnk.get("rate_mbps", 0.0))
            percent = max(0.0, min(util * 100.0, 100.0))

            key_fwd = (src, dst)
            key_rev = (dst, src)
            item = self._link_items.get(key_fwd) or self._link_items.get(key_rev)
            if item is None:
                continue

            color = util_color(util)
            width = 1.5 + util * 6.0

            pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            item["line"].setPen(pen)

            label = item["label"]
            label.setPlainText("")  # Clear old text first to prevent ghost artifacts
            if rate > 0.01:
                label_text = f"{percent:6.1f}%  {rate:6.1f}M"
                label.setPlainText(label_text)
                label.setDefaultTextColor(color.lighter(130))
            label.update()

        self._update_dots(links, flows)

    def _update_dots(self, links: list, flows: list):
        for dot in self._dots:
            dot.hide()

        flow_edge_util = {}
        for lnk in links:
            if lnk.get("host_link", False):
                continue
            src, dst = lnk["src"], lnk["dst"]
            util = float(lnk.get("util", 0.0))
            if util > UTIL_DOT_THRESHOLD:
                flow_edge_util[(src, dst)] = util
                flow_edge_util[(dst, src)] = util

        dot_idx = 0
        for (src, dst), util in list(flow_edge_util.items()):
            if dot_idx + 1 >= len(self._dots):
                break
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

        # [FONT CHANGE] Title font-size: 12px → 15px
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

        sw_links = [l for l in links if not l.get("host_link", False)]
        sorted_links = sorted(sw_links, key=lambda l: l.get("util", 0), reverse=True)

        shown = 0
        for lnk in sorted_links:
            util = max(0.0, min(float(lnk.get("util", 0)), 1.0))
            percent = max(0.0, min(util * 100.0, 100.0))
            if util < 0.01:
                continue
            color = util_color(util).name()
            row = QHBoxLayout()

            # [FONT CHANGE] Congestion row labels: 11px → 15px
            edge_lbl = QLabel(f"{lnk['src']} ↔ {lnk['dst']}")
            edge_lbl.setStyleSheet(
                "color: #8b949e; font-family: Courier New; font-size: 15px;"
            )
            util_lbl = QLabel(f"{percent:6.1f}%  {lnk.get('rate_mbps', 0):6.1f}M")
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
