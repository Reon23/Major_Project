"""
traffic_panel.py — Traffic Panel dock: start/stop iperf3 traffic flows
between any two hosts in the current topology, with adjustable bandwidth
and duration, live per-flow throughput, and independent stop per flow (or
stop-all). Talks to a TrafficManager (traffic_manager.py) via signals —
this widget has no direct knowledge of sockets/RPC.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

_STATUS_COLOR = {
    "starting": "#d69e2e",
    "running": "#38a169",
    "completed": "#9aa5b1",
    "stopped": "#9aa5b1",
    "error": "#e53e3e",
}

_COLUMNS = ["ID", "Flow", "Proto", "Target BW", "Duration", "Status", "Live Mbps", ""]


class TrafficPanel(QWidget):
    start_flow_requested = pyqtSignal(str, str, str, object, int)  # src,dst,proto,bw,duration
    stop_flow_requested = pyqtSignal(str)
    stop_all_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.connection_label = QLabel("● Not connected to network")
        self.connection_label.setStyleSheet("color: #9aa5b1; font-weight: bold;")
        layout.addWidget(self.connection_label)

        form_box = QGroupBox("New flow")
        form = QHBoxLayout(form_box)

        form.addWidget(QLabel("From:"))
        self.src_combo = QComboBox()
        form.addWidget(self.src_combo)

        form.addWidget(QLabel("To:"))
        self.dst_combo = QComboBox()
        form.addWidget(self.dst_combo)

        form.addWidget(QLabel("Proto:"))
        self.proto_combo = QComboBox()
        self.proto_combo.addItems(["UDP", "TCP"])
        form.addWidget(self.proto_combo)

        form.addWidget(QLabel("BW (Mbps):"))
        self.bw_spin = QDoubleSpinBox()
        self.bw_spin.setRange(0.1, 1000.0)
        self.bw_spin.setValue(10.0)
        form.addWidget(self.bw_spin)

        form.addWidget(QLabel("Duration (s):"))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 3600)
        self.duration_spin.setValue(10)
        form.addWidget(self.duration_spin)

        self.start_btn = QPushButton("Start Flow")
        self.start_btn.setStyleSheet("font-weight: bold;")
        self.start_btn.clicked.connect(self._on_start_clicked)
        form.addWidget(self.start_btn)

        layout.addWidget(form_box)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        bottom_row = QHBoxLayout()
        clear_btn = QPushButton("Clear finished")
        clear_btn.clicked.connect(self._clear_finished)
        stop_all_btn = QPushButton("Stop All Flows")
        stop_all_btn.clicked.connect(self.stop_all_requested.emit)
        bottom_row.addWidget(clear_btn)
        bottom_row.addStretch(1)
        bottom_row.addWidget(stop_all_btn)
        layout.addLayout(bottom_row)

        self._row_by_id: dict = {}
        self.set_connection_status(False)

    # ── host list (kept in sync with the topology editor) ───────────────

    def set_hosts(self, host_ids: list) -> None:
        for combo in (self.src_combo, self.dst_combo):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(host_ids)
            if current in host_ids:
                combo.setCurrentText(current)
            combo.blockSignals(False)

    # ── connection status ────────────────────────────────────────────────

    def set_connection_status(self, connected: bool) -> None:
        if connected:
            self.connection_label.setText("● Connected to network")
            self.connection_label.setStyleSheet("color: #38a169; font-weight: bold;")
        else:
            self.connection_label.setText("● Not connected to network")
            self.connection_label.setStyleSheet("color: #9aa5b1; font-weight: bold;")
        self.start_btn.setEnabled(connected)

    # ── form -> signal ───────────────────────────────────────────────────

    def _on_start_clicked(self) -> None:
        src = self.src_combo.currentText()
        dst = self.dst_combo.currentText()
        if not src or not dst:
            return
        if src == dst:
            return
        proto = self.proto_combo.currentText().lower()
        bw = self.bw_spin.value()
        duration = self.duration_spin.value()
        self.start_flow_requested.emit(src, dst, proto, bw, duration)

    # ── row management (driven by app.py from TrafficManager signals) ──

    def add_flow_row(
        self, fid: str, src: str, dst: str, proto: str, bw, duration: int
    ) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._row_by_id[fid] = row

        bw_text = f"{bw:g} Mbps" if bw else "—"
        values = [
            fid,
            f"{src} → {dst}",
            proto.upper(),
            bw_text,
            f"{duration}s",
            "starting",
            "—",
        ]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, col, item)
        self._paint_status(row, "starting")

        stop_btn = QPushButton("Stop")
        stop_btn.clicked.connect(lambda _checked, f=fid: self.stop_flow_requested.emit(f))
        self.table.setCellWidget(row, len(_COLUMNS) - 1, stop_btn)

    def update_flow_status(self, fid: str, status: str) -> None:
        row = self._row_by_id.get(fid)
        if row is None:
            return
        self.table.item(row, 5).setText(status)
        self._paint_status(row, status)
        if status not in ("running", "starting"):
            widget = self.table.cellWidget(row, len(_COLUMNS) - 1)
            if widget is not None:
                widget.setEnabled(False)

    def update_flow_stats(self, fid: str, mbps: float) -> None:
        row = self._row_by_id.get(fid)
        if row is None:
            return
        self.table.item(row, 6).setText(f"{mbps:.2f}")

    def mark_flow_finished(self, fid: str, reason: str) -> None:
        self.update_flow_status(fid, reason)

    def _paint_status(self, row: int, status: str) -> None:
        color = QColor(_STATUS_COLOR.get(status, "#9aa5b1"))
        self.table.item(row, 5).setForeground(color)

    def _clear_finished(self) -> None:
        keep_ids = []
        for fid, row in list(self._row_by_id.items()):
            status = self.table.item(row, 5).text()
            if status in ("running", "starting"):
                keep_ids.append(fid)
        # Rebuild the table with only the still-active rows — simplest way
        # to keep row indices consistent after removals.
        snapshot = []
        for fid in keep_ids:
            row = self._row_by_id[fid]
            snapshot.append(
                (
                    fid,
                    [self.table.item(row, c).text() for c in range(6)],
                )
            )
        self.table.setRowCount(0)
        self._row_by_id.clear()
        for fid, values in snapshot:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_by_id[fid] = row
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, item)
            self._paint_status(row, values[5])
            self.table.setItem(row, 6, QTableWidgetItem("—"))
            stop_btn = QPushButton("Stop")
            stop_btn.setEnabled(False)
            self.table.setCellWidget(row, len(_COLUMNS) - 1, stop_btn)

    def clear_all_as_stopped(self) -> None:
        """Called when the network goes down — mark every active row
        stopped since the underlying flows no longer exist."""
        for fid, row in self._row_by_id.items():
            status = self.table.item(row, 5).text()
            if status in ("running", "starting"):
                self.update_flow_status(fid, "stopped")
