"""
control_panel.py — Control Panel (start/stop/restart + status indicators)
and Logs Console docks (task doc §3).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from process_manager import ProcStatus

_STATUS_COLOR = {
    ProcStatus.NOT_STARTED: "#9aa5b1",
    ProcStatus.STARTING: "#d69e2e",
    ProcStatus.RUNNING: "#38a169",
    ProcStatus.STOPPING: "#d69e2e",
    ProcStatus.CRASHED: "#e53e3e",
}


class _StatusRow(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.dot = QLabel("\u25cf")
        self.dot.setFixedWidth(16)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: bold;")
        self.status_label = QLabel(ProcStatus.NOT_STARTED.value)
        layout.addWidget(self.dot)
        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addStretch(1)
        self.set_status(ProcStatus.NOT_STARTED)

    def set_status(self, status: ProcStatus) -> None:
        color = _STATUS_COLOR.get(status, "#9aa5b1")
        self.dot.setStyleSheet(f"color: {color};")
        self.status_label.setText(status.value)


class ControlPanel(QWidget):
    """Start/Stop/Restart for the controller and the network, plus a
    one-time sudo-password field used for the Mininet child."""

    start_all_clicked = pyqtSignal()
    stop_all_clicked = pyqtSignal()
    start_controller_clicked = pyqtSignal()
    stop_controller_clicked = pyqtSignal()
    restart_controller_clicked = pyqtSignal()
    start_mininet_clicked = pyqtSignal(str)  # password
    stop_mininet_clicked = pyqtSignal()
    restart_mininet_clicked = pyqtSignal(str)  # password

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        # ── Sudo password (Mininet needs root; see process_manager.py) ─────
        pw_box = QGroupBox("Mininet privilege (sudo)")
        pw_layout = QHBoxLayout(pw_box)
        pw_layout.addWidget(QLabel("sudo password:"))
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("leave blank if passwordless sudo is configured")
        pw_layout.addWidget(self.password_edit)
        layout.addWidget(pw_box)

        # ── Controller ───────────────────────────────────────────────────
        ctrl_box = QGroupBox("Controller (ryu-manager)")
        ctrl_layout = QVBoxLayout(ctrl_box)
        self.controller_status_row = _StatusRow("Status:")
        ctrl_layout.addWidget(self.controller_status_row)
        ctrl_btn_row = QHBoxLayout()
        self.controller_start_btn = QPushButton("Start")
        self.controller_stop_btn = QPushButton("Stop")
        self.controller_restart_btn = QPushButton("Restart")
        for b in (self.controller_start_btn, self.controller_stop_btn, self.controller_restart_btn):
            ctrl_btn_row.addWidget(b)
        ctrl_layout.addLayout(ctrl_btn_row)
        layout.addWidget(ctrl_box)

        self.controller_start_btn.clicked.connect(self.start_controller_clicked.emit)
        self.controller_stop_btn.clicked.connect(self.stop_controller_clicked.emit)
        self.controller_restart_btn.clicked.connect(self.restart_controller_clicked.emit)

        # ── Mininet ──────────────────────────────────────────────────────
        mn_box = QGroupBox("Network (Mininet)")
        mn_layout = QVBoxLayout(mn_box)
        self.mininet_status_row = _StatusRow("Status:")
        mn_layout.addWidget(self.mininet_status_row)
        mn_btn_row = QHBoxLayout()
        self.mininet_start_btn = QPushButton("Start")
        self.mininet_stop_btn = QPushButton("Stop")
        self.mininet_restart_btn = QPushButton("Restart")
        for b in (self.mininet_start_btn, self.mininet_stop_btn, self.mininet_restart_btn):
            mn_btn_row.addWidget(b)
        mn_layout.addLayout(mn_btn_row)
        layout.addWidget(mn_box)

        self.mininet_start_btn.clicked.connect(
            lambda: self.start_mininet_clicked.emit(self.password_edit.text())
        )
        self.mininet_stop_btn.clicked.connect(self.stop_mininet_clicked.emit)
        self.mininet_restart_btn.clicked.connect(
            lambda: self.restart_mininet_clicked.emit(self.password_edit.text())
        )

        # ── Start/Stop everything ───────────────────────────────────────
        all_row = QHBoxLayout()
        self.start_all_btn = QPushButton("Start Everything")
        self.start_all_btn.setStyleSheet("font-weight: bold;")
        self.stop_all_btn = QPushButton("Stop Everything")
        all_row.addWidget(self.start_all_btn)
        all_row.addWidget(self.stop_all_btn)
        layout.addLayout(all_row)
        self.start_all_btn.clicked.connect(self.start_all_clicked.emit)
        self.stop_all_btn.clicked.connect(self.stop_all_clicked.emit)

        layout.addStretch(1)

    def sudo_password(self) -> str:
        return self.password_edit.text()

    def set_controller_status(self, status: ProcStatus) -> None:
        self.controller_status_row.set_status(status)

    def set_mininet_status(self, status: ProcStatus) -> None:
        self.mininet_status_row.set_status(status)


class LogsConsole(QWidget):
    """Live stdout/stderr from both child processes, with a source filter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        header = QHBoxLayout()
        header.addWidget(QLabel("Source:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["all", "controller", "mininet", "cleanup", "traffic"])
        header.addWidget(self.filter_combo)
        header.addStretch(1)
        clear_btn = QPushButton("Clear")
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(5000)
        self.text.setStyleSheet(
            "background-color: #12161c; color: #e6e6e6; font-family: 'Courier New';"
        )
        layout.addWidget(self.text)

        clear_btn.clicked.connect(self.text.clear)

        self._lines: list = []  # (source, line) — kept so the filter can replay
        self.filter_combo.currentTextChanged.connect(self._reapply_filter)

    def append(self, source: str, line: str) -> None:
        self._lines.append((source, line))
        if len(self._lines) > 5000:
            self._lines = self._lines[-5000:]
        current_filter = self.filter_combo.currentText()
        if current_filter in ("all", source):
            self._append_line(source, line)

    def _append_line(self, source: str, line: str) -> None:
        prefix = {
            "controller": "[ctrl]",
            "mininet": "[mn]  ",
            "cleanup": "[clean]",
            "traffic": "[traf]",
        }.get(source, f"[{source}]")
        self.text.appendPlainText(f"{prefix} {line}")
        self.text.moveCursor(QTextCursor.MoveOperation.End)

    def _reapply_filter(self, source_filter: str) -> None:
        self.text.clear()
        for source, line in self._lines:
            if source_filter in ("all", source):
                self._append_line(source, line)
