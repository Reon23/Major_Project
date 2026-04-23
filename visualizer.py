"""
visualizer.py — PyQt6 Live SDN Topology Monitor
================================================
Mininet + Ryu Active Inference Controller visualizer.

Architecture
------------
  TopologyScene      – QGraphicsScene subclass; draws nodes/links, animates dots
  MetricsPanel       – QWidget with grouped QLabels / QProgressBars
  EventLog           – QPlainTextEdit wrapper with auto-scroll
  MainWindow         – QMainWindow; orchestrates layout + QTimer polling
  StateLoader        – pure function; reads / validates state.json
  DEMO_STATE         – fallback if state.json missing (allows UI preview)

Run
---
  python visualizer.py
  python visualizer.py --state /path/to/state.json
"""

import sys
import json
import math
import time
import argparse
import datetime
from pathlib import Path
from typing import Optional

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
    QProgressBar,
    QPlainTextEdit,
    QSplitter,
    QFrame,
    QSizePolicy,
)
from PyQt6.QtCore import (
    Qt,
    QTimer,
    QPointF,
    QRectF,
    QLineF,
    pyqtSignal,
    QObject,
)
from PyQt6.QtGui import (
    QPen,
    QBrush,
    QColor,
    QFont,
    QPainter,
    QLinearGradient,
    QPalette,
    QTransform,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Colour palette (dark terminal aesthetic)
# ─────────────────────────────────────────────────────────────────────────────

C_BG = QColor("#0d1117")
C_PANEL = QColor("#161b22")
C_BORDER = QColor("#30363d")
C_SWITCH = QColor("#1f6feb")
C_SWITCH_TXT = QColor("#cae8ff")
C_HOST = QColor("#238636")
C_HOST_TXT = QColor("#acf0a0")
C_LINK_IDLE = QColor("#30363d")
C_PATH0_ACT = QColor("#f78166")  # red-orange — path 0 active
C_PATH1_ACT = QColor("#56d364")  # green      — path 1 active
C_PATH_INACT = QColor("#2d3139")  # near-black
C_TEXT = QColor("#e6edf3")
C_TEXT_DIM = QColor("#8b949e")
C_UTIL_LOW = QColor("#238636")
C_UTIL_MED = QColor("#9e6a03")
C_UTIL_HIGH = QColor("#da3633")
C_DOT = QColor("#ffa657")

# ─────────────────────────────────────────────────────────────────────────────
#  Topology layout — fixed pixel positions
# ─────────────────────────────────────────────────────────────────────────────
#
#  Layout (scene coords, 620×520 canvas):
#
#         h1          h3  h4
#          \          |  |
#           s1 ──── s2 ──── s4 ── h2
#           |       \      /
#           |        s3───/
#          h5  h6   (s3)
#
#  Actual positions chosen for clarity:

NODE_POS = {
    # hosts
    "h1": (80, 120),
    "h2": (560, 240),
    "h3": (300, 60),
    "h4": (390, 60),
    "h5": (80, 400),
    "h6": (150, 400),
    # switches
    "s1": (160, 240),
    "s2": (320, 180),
    "s3": (320, 340),
    "s4": (480, 240),
}

EDGES = [
    ("h1", "s1"),
    ("h2", "s4"),
    ("h3", "s2"),
    ("h4", "s2"),
    ("h5", "s3"),
    ("h6", "s3"),
    ("s1", "s2"),
    ("s1", "s3"),
    ("s2", "s4"),
    ("s3", "s4"),
]

# Path definitions — list of EDGES that belong to each path
PATH_EDGES = {
    0: {("s1", "s2"), ("s2", "s4")},
    1: {("s1", "s3"), ("s3", "s4")},
}

# ─────────────────────────────────────────────────────────────────────────────
#  Fallback demo state (used when state.json is absent)
# ─────────────────────────────────────────────────────────────────────────────

DEMO_STATE = {
    "timestamp": "demo",
    "active_path": 0,
    "path0": {"util": 0.72, "mu": 0.70, "F": 1.12, "sigma_obs": 0.06},
    "path1": {"util": 0.15, "mu": 0.18, "F": 0.22, "sigma_obs": 0.22},
    "h1h2_load": 0.42,
    "G_stay": 1.08,
    "G_switch": 1.41,
    "P_stay": 0.93,
    "P_switch": 0.07,
    "event": "Demo mode — waiting for state.json",
}

# ─────────────────────────────────────────────────────────────────────────────
#  StateLoader — file I/O isolated here, never raises
# ─────────────────────────────────────────────────────────────────────────────


def load_state(path: str) -> tuple[dict, str]:
    """
    Returns (state_dict, error_message).
    On success error_message is ''.
    Falls back to DEMO_STATE on any failure.
    """
    p = Path(path)
    if not p.exists():
        return DEMO_STATE, "state.json not found — showing demo data"
    try:
        with p.open("r") as fh:
            data = json.load(fh)
        # Basic structural validation
        if not isinstance(data, dict):
            raise ValueError("Root must be a JSON object")
        return data, ""
    except json.JSONDecodeError as exc:
        return DEMO_STATE, f"JSON parse error: {exc}"
    except Exception as exc:
        return DEMO_STATE, f"Read error: {exc}"


def _safe(d: dict, *keys, default=0.0):
    """Safely navigate nested dict; returns default if any key missing."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ─────────────────────────────────────────────────────────────────────────────
#  Topology Scene
# ─────────────────────────────────────────────────────────────────────────────


class NodeItem:
    """Wraps the QGraphicsItem for a topology node."""

    def __init__(
        self, name: str, x: float, y: float, is_switch: bool, scene: QGraphicsScene
    ):
        self.name = name
        self.is_switch = is_switch
        W, H = (52, 30) if is_switch else (34, 34)
        self.w, self.h = W, H

        if is_switch:
            item = QGraphicsRectItem(x - W / 2, y - H / 2, W, H)
            item.setBrush(QBrush(C_SWITCH))
            item.setPen(QPen(C_SWITCH.lighter(130), 1.5))
        else:
            item = QGraphicsEllipseItem(x - W / 2, y - H / 2, W, H)
            item.setBrush(QBrush(C_HOST))
            item.setPen(QPen(C_HOST.lighter(130), 1.5))

        item.setZValue(3)
        scene.addItem(item)

        label = QGraphicsTextItem(name)
        label.setDefaultTextColor(C_SWITCH_TXT if is_switch else C_HOST_TXT)
        font = QFont("Courier New", 8, QFont.Weight.Bold)
        label.setFont(font)
        bw = label.boundingRect().width()
        bh = label.boundingRect().height()
        label.setPos(x - bw / 2, y - bh / 2)
        label.setZValue(4)
        scene.addItem(label)

        self.cx, self.cy = x, y
        self.item = item


class LinkItem:
    """Wraps a QGraphicsLineItem for a topology edge, with utilisation label."""

    def __init__(self, n1: NodeItem, n2: NodeItem, scene: QGraphicsScene):
        self.n1, self.n2 = n1, n2
        self.line = QGraphicsLineItem(n1.cx, n1.cy, n2.cx, n2.cy)
        self.line.setPen(
            QPen(C_LINK_IDLE, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        self.line.setZValue(1)
        scene.addItem(self.line)

        # Utilisation label, placed at midpoint
        mx = (n1.cx + n2.cx) / 2
        my = (n1.cy + n2.cy) / 2
        self.label = QGraphicsTextItem("")
        self.label.setDefaultTextColor(C_TEXT_DIM)
        self.label.setFont(QFont("Courier New", 7))
        self.label.setPos(mx + 4, my - 8)
        self.label.setZValue(5)
        scene.addItem(self.label)

        self._scene = scene

    def update_style(self, is_active_path: bool, path_color: QColor, util: float):
        width = 2.0 + util * 5.0  # 2px idle → 7px full utilisation
        color = path_color if is_active_path else C_PATH_INACT
        pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        self.line.setPen(pen)
        if is_active_path and util > 0:
            self.label.setPlainText(f"{util * 100:.0f}%")
            self.label.setDefaultTextColor(path_color.lighter(120))
        else:
            self.label.setPlainText("")


# Dot that travels along an edge to visualise traffic
class TrafficDot:
    def __init__(self, scene: QGraphicsScene):
        self.dot = QGraphicsEllipseItem(0, 0, 7, 7)
        self.dot.setBrush(QBrush(C_DOT))
        self.dot.setPen(QPen(Qt.PenStyle.NoPen))
        self.dot.setZValue(6)
        self.dot.setVisible(False)
        scene.addItem(self.dot)
        self.t = 0.0  # 0..1 progress along segment
        self.speed = 0.0  # fraction per timer tick

    def set_segment(self, x1: float, y1: float, x2: float, y2: float, speed=0.018):
        self.x1, self.y1 = x1, y1
        self.x2, self.y2 = x2, y2
        self.speed = speed
        self.dot.setVisible(True)

    def hide(self):
        self.dot.setVisible(False)

    def tick(self):
        self.t = (self.t + self.speed) % 1.0
        x = self.x1 + (self.x2 - self.x1) * self.t
        y = self.y1 + (self.y2 - self.y1) * self.t
        self.dot.setPos(x - 3.5, y - 3.5)


class TopologyScene(QGraphicsScene):
    """
    Draws fixed topology and updates visual state from state dict.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackgroundBrush(QBrush(C_BG))
        self.setSceneRect(0, 0, 640, 480)

        self._nodes: dict[str, NodeItem] = {}
        self._links: dict[tuple, LinkItem] = {}
        self._dots: list[TrafficDot] = []

        self._build_topology()
        self._create_dots()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_topology(self):
        SWITCHES = {"s1", "s2", "s3", "s4"}
        for name, (x, y) in NODE_POS.items():
            n = NodeItem(name, x, y, name in SWITCHES, self)
            self._nodes[name] = n

        for a, b in EDGES:
            na, nb = self._nodes[a], self._nodes[b]
            link = LinkItem(na, nb, self)
            key = (a, b)
            self._links[key] = link

    def _create_dots(self):
        # Two dots per path segment — staggered 0.5 apart for flow illusion
        for _ in range(4):
            self._dots.append(TrafficDot(self))

    # ── Update from state ─────────────────────────────────────────────────────

    def update_state(self, state: dict):
        active = int(_safe(state, "active_path", default=0))
        path_color = C_PATH0_ACT if active == 0 else C_PATH1_ACT
        active_edges = PATH_EDGES[active]

        # Per-edge path utilisation (use whichever path owns the edge)
        p0u = _safe(state, "path0", "util")
        p1u = _safe(state, "path1", "util")

        for (a, b), link in self._links.items():
            key_fwd = (a, b)
            key_rev = (b, a)
            # Determine if this edge is on an active path
            on_p0 = key_fwd in PATH_EDGES[0] or key_rev in PATH_EDGES[0]
            on_p1 = key_fwd in PATH_EDGES[1] or key_rev in PATH_EDGES[1]

            if active == 0 and on_p0:
                link.update_style(True, C_PATH0_ACT, p0u)
            elif active == 1 and on_p1:
                link.update_style(True, C_PATH1_ACT, p1u)
            else:
                # Show idle path with its dim utilisation
                idle_u = p1u if on_p0 else (p0u if on_p1 else 0.0)
                link.update_style(False, C_PATH_INACT, idle_u)

        self._animate_dots(active, path_color, p0u if active == 0 else p1u)

    def _animate_dots(self, active: int, color: QColor, util: float):
        """
        Position traffic dots on the two active-path edges.
        Strategy: hide ALL dots first, then activate only the needed ones.
        This avoids any index arithmetic about inactive edges.
        """
        # Step 1 — hide everything upfront
        for dot in self._dots:
            dot.hide()

        # Step 2 — activate one dot-pair per edge on the active path
        active_e_list = sorted(PATH_EDGES[active])  # always exactly 2 edges
        speed = 0.012 + util * 0.025

        for i, (a, b) in enumerate(active_e_list):
            n1, n2 = self._nodes[a], self._nodes[b]
            d0 = self._dots[i * 2]  # indices 0 or 2
            d1 = self._dots[i * 2 + 1]  # indices 1 or 3
            d0.set_segment(n1.cx, n1.cy, n2.cx, n2.cy, speed)
            d1.set_segment(n1.cx, n1.cy, n2.cx, n2.cy, speed)
            # Stagger second dot half a segment behind the first
            if d1.t < 0.5:
                d1.t = d0.t + 0.5
            d0.dot.setBrush(QBrush(color))
            d1.dot.setBrush(QBrush(color))

    def tick_animation(self):
        for dot in self._dots:
            if dot.dot.isVisible():
                dot.tick()


# ─────────────────────────────────────────────────────────────────────────────
#  Metrics Panel
# ─────────────────────────────────────────────────────────────────────────────


def _make_label(text="—", bold=False) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {'#e6edf3' if bold else '#8b949e'}; font-family: 'Courier New'; font-size: 11px;"
    )
    if bold:
        lbl.setStyleSheet(lbl.styleSheet() + " font-weight: bold;")
    return lbl


def _make_bar() -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 100)
    bar.setValue(0)
    bar.setTextVisible(True)
    bar.setMaximumHeight(14)
    bar.setStyleSheet("""
        QProgressBar {
            border: 1px solid #30363d;
            border-radius: 3px;
            background: #0d1117;
            color: #e6edf3;
            font-size: 9px;
            text-align: center;
        }
        QProgressBar::chunk {
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1f6feb, stop:1 #56d364);
            border-radius: 2px;
        }
    """)
    return bar


def _color_bar(bar: QProgressBar, util: float):
    """Recolour progress bar chunk based on utilisation level."""
    if util < 0.5:
        chunk = "#238636"
    elif util < 0.8:
        chunk = "#9e6a03"
    else:
        chunk = "#da3633"
    bar.setStyleSheet(
        bar.styleSheet().replace(
            "background: qlineargradient", "background: " + chunk + "; background2: "
        )
    )
    # Simpler: just set via property
    bar.setProperty(
        "util_level", "high" if util >= 0.8 else "med" if util >= 0.5 else "low"
    )


class PathGroup(QGroupBox):
    """
    A QGroupBox showing metrics for one path:
      util bar, mu, F, sigma_obs
    """

    def __init__(self, title: str, accent: QColor, parent=None):
        super().__init__(title, parent)
        self._accent = accent.name()
        self.setStyleSheet(f"""
            QGroupBox {{
                border: 1px solid {accent.name()};
                border-radius: 5px;
                margin-top: 8px;
                color: {accent.name()};
                font-family: 'Courier New';
                font-size: 11px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 12, 8, 8)

        self.util_bar = _make_bar()
        layout.addWidget(QLabel("Utilisation"))
        layout.addWidget(self.util_bar)

        self.mu_lbl = _make_label()
        self.F_lbl = _make_label()
        self.sigma_lbl = _make_label()

        for key, lbl in [
            ("μ (belief)", self.mu_lbl),
            ("F (free energy)", self.F_lbl),
            ("σ_obs", self.sigma_lbl),
        ]:
            row = QHBoxLayout()
            key_lbl = QLabel(key)
            key_lbl.setStyleSheet(
                "color: #8b949e; font-size: 10px; font-family: Courier New;"
            )
            row.addWidget(key_lbl)
            row.addStretch()
            row.addWidget(lbl)
            layout.addLayout(row)

    def refresh(self, d: dict):
        util = float(_safe(d, "util"))
        mu = float(_safe(d, "mu"))
        F = float(_safe(d, "F"))
        sigma = float(_safe(d, "sigma_obs"))
        self.util_bar.setValue(int(util * 100))
        self.util_bar.setFormat(f"{util * 100:.1f}%")
        self.mu_lbl.setText(f"{mu:.3f}")
        self.F_lbl.setText(f"{F:.4f}")
        self.sigma_lbl.setText(f"{sigma:.3f}")


class DecisionGroup(QGroupBox):
    """Shows inference / decision metrics."""

    def __init__(self, parent=None):
        super().__init__("⚙ Inference / Decision", parent)
        self.setStyleSheet("""
            QGroupBox {
                border: 1px solid #9e6a03;
                border-radius: 5px;
                margin-top: 8px;
                color: #ffa657;
                font-family: 'Courier New';
                font-size: 11px;
                font-weight: bold;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 12, 8, 8)

        self._rows: dict[str, QLabel] = {}
        fields = [
            ("h1↔h2 load", "h1h2_load"),
            ("G(stay)", "G_stay"),
            ("G(switch)", "G_switch"),
            ("P(stay)", "P_stay"),
            ("P(switch)", "P_switch"),
            ("Active path", "active_path"),
        ]
        for display, key in fields:
            row = QHBoxLayout()
            kl = QLabel(display)
            kl.setStyleSheet(
                "color: #8b949e; font-size: 10px; font-family: Courier New;"
            )
            vl = _make_label()
            self._rows[key] = vl
            row.addWidget(kl)
            row.addStretch()
            row.addWidget(vl)
            layout.addLayout(row)

    def refresh(self, state: dict):
        ap = int(_safe(state, "active_path", default=0))
        vals = {
            "h1h2_load": f"{_safe(state, 'h1h2_load'):.3f}",
            "G_stay": f"{_safe(state, 'G_stay'):.4f}",
            "G_switch": f"{_safe(state, 'G_switch'):.4f}",
            "P_stay": f"{_safe(state, 'P_stay'):.3f}",
            "P_switch": f"{_safe(state, 'P_switch'):.3f}",
            "active_path": f"path {ap}",
        }
        for key, val in vals.items():
            self._rows[key].setText(val)
        # Colour active path label
        color = C_PATH0_ACT.name() if ap == 0 else C_PATH1_ACT.name()
        self._rows["active_path"].setStyleSheet(
            f"color: {color}; font-family: 'Courier New'; font-size: 11px; font-weight: bold;"
        )


class MetricsPanel(QWidget):
    """Right-side panel: two path groups + decision group."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(240)
        self.setMaximumWidth(300)
        self.setStyleSheet(f"background-color: {C_PANEL.name()};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        title = QLabel("NETWORK METRICS")
        title.setStyleSheet(
            "color: #58a6ff; font-family: 'Courier New'; font-size: 12px;"
            "font-weight: bold; letter-spacing: 2px; border-bottom: 1px solid #30363d;"
            "padding-bottom: 6px;"
        )
        layout.addWidget(title)

        self.path0_grp = PathGroup("▶ PATH 0  (s1→s2→s4)", C_PATH0_ACT)
        self.path1_grp = PathGroup("▶ PATH 1  (s1→s3→s4)", C_PATH1_ACT)
        self.decision = DecisionGroup()

        layout.addWidget(self.path0_grp)
        layout.addWidget(self.path1_grp)
        layout.addWidget(self.decision)
        layout.addStretch()

    def refresh(self, state: dict):
        self.path0_grp.refresh(state.get("path0", {}))
        self.path1_grp.refresh(state.get("path1", {}))
        self.decision.refresh(state)


# ─────────────────────────────────────────────────────────────────────────────
#  Event Log
# ─────────────────────────────────────────────────────────────────────────────


class EventLog(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumHeight(140)
        self.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {C_BG.name()};
                color: #8b949e;
                font-family: 'Courier New';
                font-size: 10px;
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
        line = f"[{ts}]  {message}"
        self.appendPlainText(line)
        # Auto-scroll
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def log(self, message: str):
        """Always append regardless of dedup."""
        self.append_event(message, force=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Main Window
# ─────────────────────────────────────────────────────────────────────────────


class MainWindow(QMainWindow):
    def __init__(self, state_file: str = "state.json"):
        super().__init__()
        self._state_file = state_file
        self._last_event_text = ""
        self._poll_count = 0

        self._setup_window()
        self._build_ui()
        self._setup_timers()

        # Initial load
        self._poll_state()
        self._event_log.log("Visualizer started — polling " + state_file)

    # ── Window setup ─────────────────────────────────────────────────────────

    def _setup_window(self):
        self.setWindowTitle("SDN Active Inference — Live Monitor")
        self.resize(1020, 720)
        self.setMinimumSize(800, 580)

        # Dark palette
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, C_BG)
        pal.setColor(QPalette.ColorRole.WindowText, C_TEXT)
        pal.setColor(QPalette.ColorRole.Base, C_PANEL)
        pal.setColor(QPalette.ColorRole.Text, C_TEXT)
        self.setPalette(pal)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ── Header bar ───────────────────────────────────────────────────────
        header = QLabel("  ◈  SDN ACTIVE INFERENCE MONITOR")
        header.setStyleSheet(
            "color: #58a6ff; font-family: 'Courier New'; font-size: 14px;"
            "font-weight: bold; letter-spacing: 3px;"
            "background: #161b22; border-bottom: 1px solid #30363d;"
            "padding: 6px 0;"
        )
        main_layout.addWidget(header)

        # ── Middle: topology + metrics ───────────────────────────────────────
        mid_splitter = QSplitter(Qt.Orientation.Horizontal)
        mid_splitter.setStyleSheet(
            "QSplitter::handle { background: #30363d; width: 2px; }"
        )

        # Topology view
        self._scene = TopologyScene(self)
        self._view = QGraphicsView(self._scene)
        self._view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._view.setBackgroundBrush(QBrush(C_BG))
        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.fitInView(
            self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio
        )
        self._view.setMinimumWidth(500)
        mid_splitter.addWidget(self._view)

        # Metrics panel
        self._metrics = MetricsPanel()
        mid_splitter.addWidget(self._metrics)
        mid_splitter.setStretchFactor(0, 3)
        mid_splitter.setStretchFactor(1, 1)

        main_layout.addWidget(mid_splitter, stretch=1)

        # ── Event log ────────────────────────────────────────────────────────
        log_label = QLabel("EVENT LOG")
        log_label.setStyleSheet(
            "color: #8b949e; font-family: 'Courier New'; font-size: 10px;"
            "letter-spacing: 2px; padding: 2px 0;"
        )
        main_layout.addWidget(log_label)

        self._event_log = EventLog()
        main_layout.addWidget(self._event_log)

        # ── Status bar ───────────────────────────────────────────────────────
        self.statusBar().setStyleSheet(
            "color: #8b949e; font-family: 'Courier New'; font-size: 10px;"
            "background: #161b22; border-top: 1px solid #30363d;"
        )
        self.statusBar().showMessage("Initialising…")

    # ── Timers ────────────────────────────────────────────────────────────────

    def _setup_timers(self):
        # State polling every 1000 ms
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_state)
        self._poll_timer.start(1000)

        # Animation tick every 50 ms (~20 fps)
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_anim)
        self._anim_timer.start(50)

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_state(self):
        state, err = load_state(self._state_file)
        self._poll_count += 1

        # Update topology
        self._scene.update_state(state)

        # Update metrics panel
        self._metrics.refresh(state)

        # Log events (only when changed)
        event_text = str(_safe(state, "event", default=""))
        if event_text:
            self._event_log.append_event(event_text)

        if err:
            self._event_log.append_event(f"⚠  {err}")

        # Status bar
        ts = _safe(state, "timestamp", default="—")
        ap = int(_safe(state, "active_path", default=0))
        path_name = f"path {ap}"
        self.statusBar().showMessage(
            f"  Last update: {ts}   |   Active: {path_name}   |   Polls: {self._poll_count}"
        )

    # ── Animation ────────────────────────────────────────────────────────────

    def _tick_anim(self):
        self._scene.tick_animation()

    # ── Resize ───────────────────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._view.fitInView(
            self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="SDN Active Inference Visualizer")
    parser.add_argument(
        "--state",
        default="state.json",
        help="Path to the state JSON file written by the Ryu controller (default: state.json)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Apply dark base palette application-wide
    dark_palette = app.palette()
    dark_palette.setColor(QPalette.ColorRole.Window, QColor("#0d1117"))
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor("#e6edf3"))
    dark_palette.setColor(QPalette.ColorRole.Base, QColor("#161b22"))
    dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#0d1117"))
    dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#161b22"))
    dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#e6edf3"))
    dark_palette.setColor(QPalette.ColorRole.Text, QColor("#e6edf3"))
    dark_palette.setColor(QPalette.ColorRole.Button, QColor("#21262d"))
    dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e6edf3"))
    dark_palette.setColor(QPalette.ColorRole.Highlight, QColor("#1f6feb"))
    dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(dark_palette)

    win = MainWindow(state_file=args.state)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
