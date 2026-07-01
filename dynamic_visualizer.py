"""
dynamic_visualizer.py — Dynamic SDN Active Inference Visualizer (PyQt6)

Changelog (latest first):
  2026-06-19 — Three enhancements:
    1. Ledger panel: shows chain length, latest hash, and tx_type-color-coded
       transaction list with payload summaries (metrics_log, did_register,
       create_model, authorize_access_token, trade_failed, etc.).
    2. Correct packet-flow direction: dots animate along the actual flow
       path (flows[].path) instead of canonical-id order; directional
       arrowheads added on active links; reverse ACK traffic animated
       at 0.4x speed.
    3. ns-3-style packet-drop visualization: probabilistic red ✕ markers
       emitted from per-link loss_fraction, fading over 800ms; drop counter
       appended to link labels.

Polls state.json every 1s. No third-party deps beyond PyQt6 + networkx.

Future enhancement (not implemented): click-to-highlight flow paths through
a selected switch node.

Run:
    python3 dynamic_visualizer.py
    python3 dynamic_visualizer.py --state /path/to/state.json
"""

import sys
import json
import math
import random
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
    QGraphicsPathItem,
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
    QPainterPath,
)

# ── Color palette (existing) ──────────────────────────────────────────────
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

# ── Tx-type colors for the ledger panel (new) ─────────────────────────────
C_TX_DID = QColor("#58a6ff")  # genesis, did_register
C_TX_MODEL = QColor(
    "#238636"
)  # create_model, update_model_descriptions, authorize_access_token
C_TX_METRICS = QColor("#e3b341")  # metrics_log
C_TX_FAIL = QColor("#da3633")  # trade_failed, authorize_access_token_failed, withdraw_*
C_TX_OTHER = QColor("#8b949e")  # fallback

# ── Layout / behaviour constants ──────────────────────────────────────────
SCENE_W = 760
SCENE_H = 540
UTIL_DOT_THRESHOLD = 0.02
# How many consecutive polls a previously-seen switch/link may be missing
# from state.json before the visualizer actually removes it. This absorbs
# brief topology blips (e.g. a momentary EventLinkDelete under congestion)
# without ever showing a torn or half-drawn graph.
MISSING_GRACE_POLLS = 3

# ── Drop-marker tuning (new) ──────────────────────────────────────────────
DROP_FADE_MS = 800
DROP_MAX_CONCURRENT = 64
# P(at least one drop this tick) = min(1, loss_fraction * DROP_PROB_SCALE)
DROP_PROB_SCALE = 50.0

# ── Animation tick (ms) ───────────────────────────────────────────────────
ANIM_TICK_MS = 50

# ── Dot pool sizing (new) ─────────────────────────────────────────────────
# Worst case: 4 switches * 4 links * 2 directions * 2 dots = 64
DOT_POOL_SIZE = 64

# ── Reverse-direction (ACK) speed scaling (new) ───────────────────────────
ACK_SPEED_FACTOR = 0.4


# ── Demo state — shown when state.json doesn't exist yet.
# Updated to include loss_fraction on some links and a sample ledger so all
# three new visualizations are exercised in demo mode.
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
            "loss_fraction": 0.012,
        },
        {
            "src": "s1",
            "dst": "s3",
            "src_port": 3,
            "dst_port": 3,
            "rate_mbps": 1.2,
            "util": 0.12,
            "capacity_mbps": 10,
            "loss_fraction": 0.0,
        },
        {
            "src": "s2",
            "dst": "s4",
            "src_port": 4,
            "dst_port": 2,
            "rate_mbps": 4.5,
            "util": 0.45,
            "capacity_mbps": 10,
            "loss_fraction": 0.03,
        },
        {
            "src": "s3",
            "dst": "s4",
            "src_port": 4,
            "dst_port": 3,
            "rate_mbps": 0.8,
            "util": 0.08,
            "capacity_mbps": 10,
            "loss_fraction": 0.0,
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
    "ledger": {
        "chain_length": 7,
        "latest_block_hash": "9f9b00fb7acd00e5abcd1234",
        "latest_block_index": 6,
        "last_transactions": [
            {
                "index": 6,
                "tx_type": "metrics_log",
                "timestamp": 1781847697.79,
                "payload": {
                    "controller_id": "beta",
                    "src_ip": "10.0.0.1",
                    "dst_ip": "10.0.0.2",
                    "path": ["s1", "s2", "s4"],
                    "G": 0.1234,
                    "load_estimate": 0.55,
                    "ciu": 0.4863,
                    "link_losses": {"s1-s2": 0.01, "s2-s4": 0.02},
                },
                "hash": "9f9b00fb7acd",
            },
            {
                "index": 5,
                "tx_type": "authorize_access_token",
                "timestamp": 1781847515.25,
                "payload": {
                    "transaction_record": {
                        "model_id": "model_abc123def456",
                        "provider": "alpha",
                        "user": "beta",
                    },
                    "token_id": "tok_xyz789abc",
                    "issued_at": 1781847515.25,
                },
                "hash": "0818f218f625",
            },
            {
                "index": 1,
                "tx_type": "did_register",
                "timestamp": 1781847515.24,
                "payload": {
                    "did": "alpha",
                    "public_key": "4b87a9b38254c51268c68178c25545ed6c8dd6a91f728d7701a50b60ed227b6b",
                    "encryption_algorithm": "Ed25519",
                    "managed_dpids": [1, 2],
                },
                "hash": "435647cb7564",
            },
        ],
    },
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
    switch_ids = {n["id"] for n in nodes if n.get("type") == "switch"}
    host_ids = {n["id"] for n in nodes if n.get("type") != "switch"}

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

    host_switch = {}
    for lnk in links:
        if lnk.get("host_link"):
            h = lnk["src"] if lnk["src"] in host_ids else lnk["dst"]
            sw = lnk["dst"] if lnk["src"] in host_ids else lnk["src"]
            if sw in result:
                host_switch[h] = sw

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


def _make_arrow_path(
    x: float, y: float, angle_rad: float, size: float = 10.0
) -> QPainterPath:
    """
    Return a QPainterPath forming a filled triangle at (x, y) pointing
    in direction `angle_rad` (radians). The tip is at (x, y); the base
    sits behind, perpendicular to the direction vector.
    """
    path = QPainterPath()
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    wing = size * 0.55

    def tf(lx, ly):
        rx = lx * cos_a - ly * sin_a
        ry = lx * sin_a + ly * cos_a
        return (x + rx, y + ry)

    p1 = tf(0.0, 0.0)  # tip
    p2 = tf(-size, -wing)  # base-left
    p3 = tf(-size, wing)  # base-right

    path.moveTo(*p1)
    path.lineTo(*p2)
    path.lineTo(*p3)
    path.closeSubpath()
    return path


# ── TrafficDot — small animated circle along a link segment ───────────────


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


# ── DropMarker — ns-3-style red ✕ that fades over DROP_FADE_MS ────────────


class DropMarker:
    """
    A small red "✕" rendered as two short QLineItems, fading out over
    `lifetime_ms`. Tick returns False once fully faded (caller should
    then remove it from the scene).
    """

    SIZE = 6  # half-length of each arm of the X

    def __init__(
        self, scene: QGraphicsScene, x: float, y: float, lifetime_ms: int = DROP_FADE_MS
    ):
        pen = QPen(C_LINK_HI, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        self.line1 = QGraphicsLineItem(
            x - self.SIZE, y - self.SIZE, x + self.SIZE, y + self.SIZE
        )
        self.line2 = QGraphicsLineItem(
            x - self.SIZE, y + self.SIZE, x + self.SIZE, y - self.SIZE
        )
        for l in (self.line1, self.line2):
            l.setPen(pen)
            l.setZValue(8)
            scene.addItem(l)
        self.age_ms = 0
        self.lifetime_ms = max(1, lifetime_ms)

    def tick(self, dt_ms: int) -> bool:
        """Advance age, update opacity. Returns True if still visible."""
        self.age_ms += dt_ms
        opacity = max(0.0, 1.0 - self.age_ms / self.lifetime_ms)
        self.line1.setOpacity(opacity)
        self.line2.setOpacity(opacity)
        return opacity > 0.0

    def remove(self, scene: QGraphicsScene):
        scene.removeItem(self.line1)
        scene.removeItem(self.line2)


# ── TopologyScene — owns the graph and all its QGraphicsItems ─────────────


class TopologyScene(QGraphicsScene):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackgroundBrush(QBrush(C_BG))
        self.setSceneRect(0, 0, SCENE_W, SCENE_H)

        self._node_items: dict = {}
        # _link_items keyed by canonical (small, large) pair.
        # Each entry: {"line", "label", "host_link",
        #              "arrows": {directed_key: QGraphicsPathItem}}
        self._link_items: dict = {}
        self._dots: list = []
        self._last_state: dict = {}
        # Topology signature based only on node set + undirected edge set,
        # not on directed src/dst order — prevents spurious rebuilds.
        self._topo_sig: str = ""
        self._known_node_ids: set = set()
        self._known_link_keys: set = set()
        self._missing_node_count: dict = {}
        self._missing_link_count: dict = {}
        self._node_type_cache: dict = {}
        self._node_ip_cache: dict = {}

        # Drop-marker state (new).
        self._drop_markers: list = []
        # Per-link RNG seeded by canonical key so markers don't all fire
        # on the same tick across all links.
        self._drop_rngs: dict = {}

        for _ in range(DOT_POOL_SIZE):
            self._dots.append(TrafficDot(self))

    # ── Topology signature & rebuild ──────────────────────────────────────

    def _topo_signature(self, nodes: list, links: list) -> str:
        n_ids = sorted(n["id"] for n in nodes)
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
            for arrow in items.get("arrows", {}).values():
                self.removeItem(arrow)
        self._link_items.clear()

        # Clear all drop markers — they're at stale coordinates after a rebuild.
        for m in self._drop_markers:
            m.remove(self)
        self._drop_markers.clear()

        positions = compute_layout(nodes, links)

        for n in nodes:
            nid = n["id"]
            is_switch = n.get("type") == "switch"
            pos = positions.get(nid)
            if pos is None:
                continue
            cx, cy = pos
            self._draw_node(nid, cx, cy, is_switch, n.get("ip", ""))

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

        self._link_items[key] = {
            "line": line,
            "label": lbl,
            "host_link": is_host_link,
            "arrows": {},  # directed_key (a, b) -> QGraphicsPathItem
        }

    def _apply_missing_grace(self, nodes: list, links: list) -> tuple:
        """
        Merge the just-received nodes/links with the last-known-good set so
        that anything missing for fewer than MISSING_GRACE_POLLS consecutive
        polls is kept (using its last-known data) instead of being torn down
        immediately. Returns the (possibly patched) nodes, links to render.
        """
        incoming_node_ids = {n["id"] for n in nodes}
        incoming_link_keys = {_canonical_link_key(l["src"], l["dst"]) for l in links}

        for n in nodes:
            self._node_type_cache[n["id"]] = n.get("type", "switch")
            if n.get("ip"):
                self._node_ip_cache[n["id"]] = n["ip"]

        for nid in list(self._known_node_ids):
            if nid in incoming_node_ids:
                self._missing_node_count.pop(nid, None)
                continue
            miss = self._missing_node_count.get(nid, 0) + 1
            self._missing_node_count[nid] = miss
            if miss <= MISSING_GRACE_POLLS:
                node_type = self._node_type_cache.get(nid, "switch")
                patched = {"id": nid, "type": node_type}
                if nid in self._node_ip_cache:
                    patched["ip"] = self._node_ip_cache[nid]
                nodes = nodes + [patched]
                incoming_node_ids.add(nid)
            else:
                self._known_node_ids.discard(nid)

        self._known_node_ids |= incoming_node_ids

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

    # ── Per-poll update ──────────────────────────────────────────────────

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
        self._emit_drop_markers(links)

    def _refresh_links(self, links: list, flows: list):
        """
        Update switch-to-switch link colour/thickness, util+loss label,
        arrowheads, and dot animation. Util/loss are merged by canonical
        key (max of both directions).
        """
        # Build canonical util+loss map from all link entries in state.json.
        util_map: dict = {}
        for lnk in links:
            if lnk.get("host_link", False):
                continue
            src, dst = lnk["src"], lnk["dst"]
            key = _canonical_link_key(src, dst)
            util = max(0.0, min(float(lnk.get("util", 0.0)), 1.0))
            rate = float(lnk.get("rate_mbps", 0.0))
            loss = max(0.0, min(float(lnk.get("loss_fraction", 0.0)), 1.0))
            if key not in util_map:
                util_map[key] = {"util": util, "rate": rate, "loss": loss}
            else:
                e = util_map[key]
                e["util"] = max(e["util"], util)
                e["rate"] = max(e["rate"], rate)
                e["loss"] = max(e["loss"], loss)

        # Update link visuals + labels (with optional drop counter suffix).
        for key, data in util_map.items():
            item = self._link_items.get(key)
            if item is None:
                continue

            util = data["util"]
            rate = data["rate"]
            loss = data["loss"]
            percent = util * 100.0

            color = util_color(util)
            width = 1.5 + util * 6.0
            pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            item["line"].setPen(pen)

            # Build label — possibly with two colors (util part + drop part).
            util_text = f"{percent:5.1f}%  {rate:5.1f}M" if rate > 0.01 else ""

            if util_text or loss > 0.0:
                parts = []
                if util_text:
                    parts.append(
                        f'<span style="color:{color.lighter(130).name()}; '
                        f'font-family:Courier New; font-size:7pt">'
                        f"{util_text}</span>"
                    )
                if loss > 0.0:
                    loss_pct = loss * 100.0
                    loss_color = C_LINK_HI.name() if loss > 0.01 else C_TEXT_DIM.name()
                    parts.append(
                        f'<span style="color:{loss_color}; '
                        f'font-family:Courier New; font-size:7pt">'
                        f"  drop={loss_pct:.2f}%</span>"
                    )
                item["label"].setHtml("".join(parts))
            else:
                item["label"].setPlainText("")
            item["label"].update()

        # Links not present in util_map → reset.
        for key, item in self._link_items.items():
            if item.get("host_link"):
                continue
            if key not in util_map:
                item["line"].setPen(QPen(C_LINK_OFF, 2, Qt.PenStyle.SolidLine))
                item["label"].setPlainText("")
                # Clear arrows on inactive links.
                for arrow in item.get("arrows", {}).values():
                    arrow.setVisible(False)

        # Build directed flow map and update arrowheads + dots.
        directed_flows = self._build_directed_flow_map(flows)
        self._update_arrowheads(directed_flows, util_map)
        self._update_dots(directed_flows, util_map)

    def _build_directed_flow_map(self, flows: list) -> dict:
        """
        Returns: dict[(src_id, dst_id)] -> float weight

        Each flow contributes weight 1.0 to each directed edge along its
        forward path (h_src → s1 → ... → sN → h_dst), and weight
        ACK_SPEED_FACTOR (0.4) to each directed edge along the reverse
        path (representing ACK / control traffic).

        Robust to:
          - flows[] being empty → returns {}
          - flows[].path having length 1 → only host→switch→host edges
          - host node ids not matching the h_<ip> convention → host
            endpoints skipped, switch path still animated
          - flows[].path referring to a switch id not in nodes → that
            hop skipped, the rest animated
        """
        directed: dict = {}
        valid_ids = set(self._node_items.keys())

        for flow in flows:
            path = flow.get("path", [])
            src_ip = flow.get("src_ip", "")
            dst_ip = flow.get("dst_ip", "")
            src_host = f"h_{src_ip}" if src_ip else None
            dst_host = f"h_{dst_ip}" if dst_ip else None

            # Build full directed path including hosts (skip unknown ids).
            full_path = []
            if src_host and src_host in valid_ids:
                full_path.append(src_host)
            for sw in path:
                if sw in valid_ids:
                    full_path.append(sw)
            if dst_host and dst_host in valid_ids:
                full_path.append(dst_host)

            # Forward: weight 1.0 per hop.
            for i in range(len(full_path) - 1):
                a, b = full_path[i], full_path[i + 1]
                if a == b:
                    continue
                directed[(a, b)] = directed.get((a, b), 0.0) + 1.0

            # Reverse (ACK): weight ACK_SPEED_FACTOR per hop.
            rev_path = list(reversed(full_path))
            for i in range(len(rev_path) - 1):
                a, b = rev_path[i], rev_path[i + 1]
                if a == b:
                    continue
                directed[(a, b)] = directed.get((a, b), 0.0) + ACK_SPEED_FACTOR

        return directed

    def _update_arrowheads(self, directed_flows: dict, util_map: dict):
        """
        For each directed edge in `directed_flows`, ensure an arrowhead
        is visible at the 60% point of the canonical link line, oriented
        along the direction. If both directions are active on the same
        canonical link, draw two arrowheads offset perpendicularly by 6px.

        Skip arrowheads on host links (no directional congestion visualization
        on access links — only on inter-switch trunks).
        """
        # First, hide all existing arrows.
        for item in self._link_items.values():
            for arrow in item.get("arrows", {}).values():
                arrow.setVisible(False)

        # Group directed edges by canonical key so we can offset bidirectional
        # arrows perpendicularly.
        by_canonical: dict = {}
        for (a, b), w in directed_flows.items():
            if a not in self._node_items or b not in self._node_items:
                continue
            ckey = _canonical_link_key(a, b)
            by_canonical.setdefault(ckey, []).append(((a, b), w))

        for ckey, directs in by_canonical.items():
            item = self._link_items.get(ckey)
            if item is None or item.get("host_link"):
                continue
            n1 = self._node_items[ckey[0]]
            n2 = self._node_items[ckey[1]]

            x1, y1 = n1["cx"], n1["cy"]
            x2, y2 = n2["cx"], n2["cy"]
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            if length < 1.0:
                continue
            ux, uy = dx / length, dy / length
            # Perpendicular unit vector (for offset).
            px, py = -uy, ux

            util = util_map.get(ckey, {}).get("util", 0.0)
            color = util_color(util)

            offset_mag = 6.0 if len(directs) > 1 else 0.0

            for idx, ((a, b), w) in enumerate(directs):
                # Direction sign relative to canonical orientation
                # (small → large). If (a,b) == ckey, forward; else reverse.
                if (a, b) == ckey:
                    sign = +1.0
                else:
                    sign = -1.0

                # 60% along canonical direction (small → large).
                t = 0.6
                base_x = x1 + dx * t
                base_y = y1 + dy * t
                # Perpendicular offset; alternate sign for the two arrows.
                side = +1 if idx == 0 else -1
                base_x += px * offset_mag * side
                base_y += py * offset_mag * side

                # Arrow angle: along (a → b) direction.
                angle = math.atan2(sign * dy, sign * dx)

                dkey = (a, b)
                arrow = item["arrows"].get(dkey)
                if arrow is None:
                    arrow = QGraphicsPathItem()
                    arrow.setPen(QPen(Qt.PenStyle.NoPen))
                    arrow.setZValue(6)
                    self.addItem(arrow)
                    item["arrows"][dkey] = arrow

                arrow.setPath(_make_arrow_path(base_x, base_y, angle, size=10))
                arrow.setBrush(QBrush(color))
                arrow.setVisible(True)

    def _update_dots(self, directed_flows: dict, util_map: dict):
        """
        Allocate dots per directed edge. Each active directed edge gets
        2 dots. Speed scales with util (existing behaviour) and is
        multiplied by the edge's flow-weight ratio (forward=full,
        reverse=ACK_SPEED_FACTOR).
        """
        for dot in self._dots:
            dot.hide()

        dot_idx = 0
        for (a, b), weight in directed_flows.items():
            if dot_idx + 1 >= len(self._dots):
                break
            if a not in self._node_items or b not in self._node_items:
                continue

            n1 = self._node_items[a]
            n2 = self._node_items[b]

            ckey = _canonical_link_key(a, b)
            util = util_map.get(ckey, {}).get("util", 0.0)
            # Skip edges with no real traffic (util below threshold AND
            # weight below 0.5, i.e. only ACK traffic on an idle link).
            if util < UTIL_DOT_THRESHOLD and weight < 0.5:
                continue

            color = util_color(util)
            # Speed: base + util*range, scaled by weight (forward=full,
            # reverse=slower). Clamp to a sane range.
            base_speed = 0.010 + util * 0.025
            speed = base_speed * (1.0 if weight >= 0.5 else ACK_SPEED_FACTOR)
            speed = max(0.004, min(0.04, speed))

            d0 = self._dots[dot_idx]
            d1 = self._dots[dot_idx + 1]
            dot_idx += 2

            d0.set_segment(n1["cx"], n1["cy"], n2["cx"], n2["cy"], speed)
            d1.set_segment(n1["cx"], n1["cy"], n2["cx"], n2["cy"], speed)
            if d1.t < 0.5:
                d1.t = d0.t + 0.5
            d0.dot.setBrush(QBrush(color))
            d1.dot.setBrush(QBrush(color))

    # ── Drop-marker emission (new) ───────────────────────────────────────

    def _emit_drop_markers(self, links: list):
        """
        Called once per poll (1 Hz). For each switch-to-switch link with
        loss_fraction > 0, probabilistically emit 1+ drop markers along
        the line segment. Uses a per-link RNG (seeded by canonical key)
        so markers don't all fire on the same tick across all links.
        """
        # Aggregate loss by canonical key (max of both directions).
        loss_by_key: dict = {}
        for lnk in links:
            if lnk.get("host_link", False):
                continue
            src, dst = lnk["src"], lnk["dst"]
            key = _canonical_link_key(src, dst)
            loss = max(0.0, min(float(lnk.get("loss_fraction", 0.0)), 1.0))
            if key not in loss_by_key or loss > loss_by_key[key]:
                loss_by_key[key] = loss

        for key, loss in loss_by_key.items():
            if loss <= 0.0:
                continue
            item = self._link_items.get(key)
            if item is None or item.get("host_link"):
                continue

            # Per-link RNG seeded by a stable hash of the canonical key.
            if key not in self._drop_rngs:
                seed = sum(ord(c) for c in (key[0] + key[1])) & 0xFFFFFFFF
                self._drop_rngs[key] = random.Random(seed)
            rng = self._drop_rngs[key]

            # P(at least one drop this tick) = min(1, loss * DROP_PROB_SCALE)
            p_drop = min(1.0, loss * DROP_PROB_SCALE)
            if rng.random() > p_drop:
                continue

            # Number of markers this tick: 1 + int(loss * 100), capped at 3.
            n_markers = min(3, 1 + int(loss * 100))

            n1 = self._node_items.get(key[0])
            n2 = self._node_items.get(key[1])
            if n1 is None or n2 is None:
                continue
            x1, y1 = n1["cx"], n1["cy"]
            x2, y2 = n2["cx"], n2["cy"]
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            if length < 1.0:
                continue
            pxn, pyn = -dy / length, dx / length  # perpendicular unit

            for _ in range(n_markers):
                # Bias toward the receiving end (t values in [0.55, 0.95]).
                t = 0.55 + rng.random() * 0.40
                # Small perpendicular jitter for visibility.
                jitter = (rng.random() - 0.5) * 4.0
                mx = x1 + dx * t + pxn * jitter
                my = y1 + dy * t + pyn * jitter

                # Enforce concurrent-marker cap (drop oldest).
                if len(self._drop_markers) >= DROP_MAX_CONCURRENT:
                    oldest = self._drop_markers.pop(0)
                    oldest.remove(self)

                self._drop_markers.append(DropMarker(self, mx, my))

    # ── Animation tick ───────────────────────────────────────────────────

    def tick_animation(self):
        for dot in self._dots:
            if dot.dot.isVisible():
                dot.tick()

        # Tick drop markers; remove faded ones.
        if self._drop_markers:
            still_alive = []
            for m in self._drop_markers:
                if m.tick(ANIM_TICK_MS):
                    still_alive.append(m)
                else:
                    m.remove(self)
            self._drop_markers = still_alive


# ── Side-panel helpers (existing + new tx-color/summary) ──────────────────


def _styled_label(text="—", bold=False, color="#8b949e") -> QLabel:
    lbl = QLabel(text)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(
        f"color: {color}; font-family: 'Courier New'; font-size: 15px; font-weight: {weight};"
    )
    return lbl


def _tx_color(tx_type: str) -> QColor:
    """Color-code a transaction by its tx_type for the ledger panel."""
    if tx_type in ("genesis", "did_register"):
        return C_TX_DID
    if tx_type in (
        "create_model",
        "update_model_descriptions",
        "authorize_access_token",
    ):
        return C_TX_MODEL
    if tx_type == "metrics_log":
        return C_TX_METRICS
    if tx_type in (
        "trade_failed",
        "authorize_access_token_failed",
        "withdraw_model",
        "withdraw_verifi_tools",
    ):
        return C_TX_FAIL
    return C_TX_OTHER


def _tx_summary(tx_type: str, payload: dict) -> str:
    """One-line, tx_type-specific payload summary for the ledger panel."""
    if not isinstance(payload, dict):
        return ""
    if tx_type == "genesis":
        return str(payload.get("note", ""))[:40]
    if tx_type == "did_register":
        did = payload.get("did", "?")
        pub = str(payload.get("public_key", ""))[:8]
        return f"{did}  pub={pub}…"
    if tx_type == "create_model":
        mid = str(payload.get("model_id", "?"))[:12]
        owner = payload.get("owner", "?")
        return f"model={mid}  owner={owner}"
    if tx_type == "update_model_descriptions":
        mid = str(payload.get("model_id", "?"))[:12]
        h = str(payload.get("hash", ""))[:8]
        return f"model={mid}  hash={h}…"
    if tx_type == "authorize_access_token":
        tr = payload.get("transaction_record", {}) or {}
        provider = tr.get("provider", "?")
        user = tr.get("user", "?")
        mid = str(tr.get("model_id", "?"))[:12]
        return f"{provider} → {user}  model={mid}"
    if tx_type == "metrics_log":
        src = payload.get("src_ip", "?")
        dst = payload.get("dst_ip", "?")
        load = payload.get("load_estimate", 0.0)
        ciu = payload.get("ciu")
        G = payload.get("G", 0.0)
        try:
            load_f = float(load)
            G_f = float(G)
        except (TypeError, ValueError):
            load_f, G_f = 0.0, 0.0
        if ciu is not None:
            try:
                ciu_f = float(ciu)
                return f"{src} → {dst}  load={load_f:.2f}  CIU={ciu_f:.2f}  G={G_f:.2f}"
            except (TypeError, ValueError):
                pass
        return f"{src} → {dst}  load={load_f:.2f}  G={G_f:.2f}"
    if tx_type == "trade_failed":
        return (
            f"{payload.get('provider', '?')} → {payload.get('user', '?')}  "
            f"reason={payload.get('reason', '?')}"
        )
    if tx_type == "authorize_access_token_failed":
        return (
            f"{payload.get('provider', '?')} → {payload.get('user', '?')}  "
            f"reason={payload.get('reason', '?')}"
        )
    if tx_type == "withdraw_model":
        return f"model={str(payload.get('model_id', '?'))[:12]}"
    if tx_type in (
        "publish_verifi_tools",
        "update_verifi_tools",
        "withdraw_verifi_tools",
    ):
        pk = str(payload.get("public_key", payload.get("new_public_key", "")))[:8]
        return f"pub={pk}…"
    # Fallback: first key:value pair, truncated.
    for k, v in payload.items():
        return f"{k}={str(v)[:40]}"
    return ""


# ── SidePanel — congested links + new blockchain ledger panel ─────────────


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

        # ── Existing: Top Congested Links ───────────────────────────────
        cong_box = QGroupBox("▲ Top Congested Links")
        cong_box.setStyleSheet(self._gbox_style("#da3633"))
        self._cong_layout = QVBoxLayout(cong_box)
        self._cong_layout.setSpacing(4)
        self._cong_layout.setContentsMargins(8, 12, 8, 8)
        outer.addWidget(cong_box, stretch=1)

        # ── New: Blockchain Ledger panel ────────────────────────────────
        ledger_box = QGroupBox("⛓  Blockchain Ledger")
        ledger_box.setStyleSheet(self._gbox_style("#58a6ff"))
        ledger_layout = QVBoxLayout(ledger_box)
        ledger_layout.setSpacing(4)
        ledger_layout.setContentsMargins(8, 12, 8, 8)

        # Header row: chain # + length + truncated hash.
        self._ledger_header = QLabel("Ledger disabled")
        self._ledger_header.setStyleSheet(
            "color: #8b949e; font-family: 'Courier New'; font-size: 12px;"
        )
        self._ledger_header.setWordWrap(True)
        ledger_layout.addWidget(self._ledger_header)

        # Scrollable transaction list.
        self._tx_scroll = QScrollArea()
        self._tx_scroll.setWidgetResizable(True)
        self._tx_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._tx_scroll.setStyleSheet("background: transparent;")

        tx_container = QWidget()
        self._tx_layout = QVBoxLayout(tx_container)
        self._tx_layout.setSpacing(2)
        self._tx_layout.setContentsMargins(0, 0, 0, 0)
        self._tx_layout.addStretch()
        self._tx_scroll.setWidget(tx_container)

        ledger_layout.addWidget(self._tx_scroll, stretch=1)
        outer.addWidget(ledger_box, stretch=1)

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

        # ── Top Congested Links (unchanged) ─────────────────────────────
        self._clear_layout(self._cong_layout)

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
                    "label": f"{min(src, dst)} ↔ {max(src, dst)}",
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

        # ── Blockchain Ledger panel (new) ──────────────────────────────
        ledger = state.get("ledger")
        if not ledger or not isinstance(ledger, dict):
            self._ledger_header.setText("Ledger disabled")
            self._ledger_header.setStyleSheet(
                "color: #8b949e; font-family: 'Courier New'; font-size: 12px;"
            )
            self._clear_layout(self._tx_layout)
            self._tx_layout.addStretch()
            return

        chain_length = ledger.get("chain_length", 0)
        latest_idx = ledger.get("latest_block_index", 0)
        latest_hash = ledger.get("latest_block_hash", "")
        hash_disp = (latest_hash[:12] + "…") if len(latest_hash) > 12 else latest_hash

        self._ledger_header.setText(
            f"Chain #{latest_idx}  |  len={chain_length}  |  {hash_disp}"
        )
        self._ledger_header.setStyleSheet(
            "color: #58a6ff; font-family: 'Courier New'; font-size: 12px;"
        )

        # Rebuild transaction list (newest first).
        self._clear_layout(self._tx_layout)
        txs = ledger.get("last_transactions", [])
        # last_transactions is newest-last in our schema; display newest-first.
        for tx in reversed(txs):
            if not isinstance(tx, dict):
                continue
            tx_type = tx.get("tx_type", "?")
            idx = tx.get("index", "?")
            payload = tx.get("payload", {}) or {}
            summary = _tx_summary(tx_type, payload)
            color = _tx_color(tx_type).name()

            row = QHBoxLayout()
            row.setSpacing(6)
            idx_lbl = QLabel(f"#{idx}")
            idx_lbl.setStyleSheet(
                "color: #8b949e; font-family: 'Courier New'; font-size: 11px;"
            )
            type_lbl = QLabel(tx_type)
            type_lbl.setStyleSheet(
                f"color: {color}; font-family: 'Courier New'; "
                f"font-size: 11px; font-weight: bold;"
            )
            sum_lbl = QLabel(summary)
            sum_lbl.setStyleSheet(
                "color: #e6edf3; font-family: 'Courier New'; font-size: 11px;"
            )
            sum_lbl.setWordWrap(True)

            row.addWidget(idx_lbl)
            row.addWidget(type_lbl)
            row.addWidget(sum_lbl, stretch=1)
            self._tx_layout.addLayout(row)

        self._tx_layout.addStretch()


# ── EventLog (unchanged) ──────────────────────────────────────────────────


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


# ── MainWindow (essentially unchanged; uses ANIM_TICK_MS constant) ────────


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
        self._anim_timer.start(ANIM_TICK_MS)

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
