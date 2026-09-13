#!/usr/bin/env python3
"""
app.py — Single-process orchestrator for the SDN Active-Inference demo.

Replaces the three manual terminals:

    ryu-manager --observe-links active_inference_dynamic.py
    sudo python3 topology.py
    python3 dynamic_visualizer.py

...with one GUI process. Extends dynamic_visualizer.MainWindow (task doc
§3) with three new docks:

    - Control Panel  — start/stop/restart the controller + Mininet child
                        processes, with status indicators.
    - Topology Editor — visual editor to add/edit/delete switches, hosts,
                        and links, then Apply.
    - Logs Console    — live stdout/stderr from both children.

Run it with:

    python3 app.py

The GUI itself runs as a normal user; only the Mininet child process is
escalated via sudo (see process_manager.py). Closing the window terminates
both children and cleans up any Mininet/OVS state (`mn -c` equivalent) so
the next launch never needs a manual cleanup step.

The original three scripts remain individually runnable as a fallback —
this file only composes them, it doesn't change how they work standalone.
"""

from __future__ import annotations

import os
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent, QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QMessageBox,
)

from control_panel import ControlPanel, LogsConsole
from dynamic_visualizer import MainWindow
from process_manager import ProcessManager, ProcStatus
from sdn.topology_spec import DEFAULT_SPEC_PATH, default_spec, save_spec
from topology_editor import TopologyEditorWidget
from traffic_manager import TrafficManager
from traffic_panel import TrafficPanel

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(PROJECT_DIR, DEFAULT_SPEC_PATH)
STATE_PATH = os.path.join(PROJECT_DIR, "state.json")


class OrchestratorWindow(MainWindow):
    def __init__(self):
        # Ensure a spec file exists before anything starts, so the
        # controller's very first _reload_topology_spec() and a manual
        # `python3 topology.py --spec topology_spec.json` both have
        # something valid to read (task doc §4 "default preset").
        if not os.path.exists(SPEC_PATH):
            save_spec(default_spec(), SPEC_PATH)

        super().__init__(state_file=STATE_PATH)
        self.setWindowTitle(
            "SDN Active Inference — Orchestrator (controller + network + monitor)"
        )

        self.process_manager = ProcessManager(project_dir=PROJECT_DIR, parent=self)
        self.traffic_manager = TrafficManager(parent=self)
        self._build_docks()
        self._wire_process_manager()
        self._wire_traffic_manager()
        self._refresh_traffic_hosts()

        self._event_log.log(
            "Orchestrator ready. Use the Control Panel to start the "
            "controller and network, or the Topology Editor to change the "
            "topology first."
        )

    # ── Docks ────────────────────────────────────────────────────────────

    def _build_docks(self) -> None:
        self.control_panel = ControlPanel()
        control_dock = QDockWidget("Control Panel", self)
        control_dock.setWidget(self.control_panel)
        control_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, control_dock)

        self.topology_editor = TopologyEditorWidget(SPEC_PATH)
        editor_dock = QDockWidget("Topology Editor", self)
        editor_dock.setWidget(self.topology_editor)
        editor_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, editor_dock)
        self.tabifyDockWidget(control_dock, editor_dock)
        control_dock.raise_()

        self.logs_console = LogsConsole()
        logs_dock = QDockWidget("Logs Console", self)
        logs_dock.setWidget(self.logs_console)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, logs_dock)

        self.traffic_panel = TrafficPanel()
        traffic_dock = QDockWidget("Traffic Generator", self)
        traffic_dock.setWidget(self.traffic_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, traffic_dock)
        self.tabifyDockWidget(logs_dock, traffic_dock)
        traffic_dock.raise_()

        self.resize(1500, 900)

    # ── Wiring ───────────────────────────────────────────────────────────

    def _wire_process_manager(self) -> None:
        pm = self.process_manager
        cp = self.control_panel

        pm.controller_status_changed.connect(cp.set_controller_status)
        pm.mininet_status_changed.connect(cp.set_mininet_status)
        pm.controller_log.connect(lambda line: self.logs_console.append("controller", line))
        pm.mininet_log.connect(lambda line: self.logs_console.append("mininet", line))
        pm.cleanup_log.connect(lambda line: self.logs_console.append("cleanup", line))
        pm.controller_crashed.connect(
            lambda reason: self._on_crash("Controller", reason)
        )
        pm.mininet_crashed.connect(lambda reason: self._on_crash("Mininet", reason))

        cp.start_controller_clicked.connect(lambda: pm.start_controller(SPEC_PATH))
        cp.stop_controller_clicked.connect(pm.stop_controller)
        cp.restart_controller_clicked.connect(lambda: pm.restart_controller(SPEC_PATH))

        cp.start_mininet_clicked.connect(self._start_mininet)
        cp.stop_mininet_clicked.connect(pm.stop_mininet)
        cp.restart_mininet_clicked.connect(self._restart_mininet)

        cp.start_all_clicked.connect(self._start_everything)
        cp.stop_all_clicked.connect(self._stop_everything)

        self.topology_editor.apply_requested.connect(self._apply_topology)
        self.topology_editor.scene.model_changed.connect(self._refresh_traffic_hosts)

        # Mininet status also drives whether the Traffic Panel can talk to
        # the network's traffic RPC server (task: "send traffic between
        # hosts from the Control Panel").
        pm.mininet_status_changed.connect(self._on_mininet_status_for_traffic)

    def _on_mininet_status_for_traffic(self, status: ProcStatus) -> None:
        if status == ProcStatus.RUNNING:
            # The traffic RPC server starts inside the Mininet process
            # right before it prints the "mininet>" prompt (which is what
            # flips this status to RUNNING) — a short delay avoids a
            # doomed-to-fail first connection attempt on a slow machine.
            QTimer.singleShot(1500, self.traffic_manager.connect_to_server)
        else:
            self.traffic_manager.disconnect_from_server()
            self.traffic_panel.set_connection_status(False)
            self.traffic_panel.clear_all_as_stopped()

    def _wire_traffic_manager(self) -> None:
        tm = self.traffic_manager
        tp = self.traffic_panel

        tm.connected.connect(lambda: tp.set_connection_status(True))
        tm.disconnected.connect(lambda: tp.set_connection_status(False))
        tm.connection_error.connect(
            lambda msg: self.logs_console.append("traffic", f"ERROR: {msg}")
        )
        tm.flow_ack.connect(self._on_flow_ack)
        tm.flow_stats.connect(
            lambda fid, mbps, interval: tp.update_flow_stats(fid, mbps)
        )
        tm.flow_log.connect(
            lambda fid, line: self.logs_console.append("traffic", f"{fid}: {line}")
        )
        tm.flow_finished.connect(tp.mark_flow_finished)

        tp.start_flow_requested.connect(self._start_traffic_flow)
        tp.stop_flow_requested.connect(tm.stop_flow)
        tp.stop_all_requested.connect(tm.stop_all)

    def _start_traffic_flow(
        self, src: str, dst: str, proto: str, bw: float, duration: int
    ) -> None:
        fid = self.traffic_manager.start_flow(src, dst, proto, bw, duration)
        self.traffic_panel.add_flow_row(fid, src, dst, proto, bw, duration)

    def _on_flow_ack(self, fid: str, ok: bool, error: str) -> None:
        if ok:
            self.traffic_panel.update_flow_status(fid, "running")
        else:
            self.traffic_panel.update_flow_status(fid, "error")
            self.logs_console.append("traffic", f"{fid}: failed to start — {error}")

    def _refresh_traffic_hosts(self) -> None:
        hosts = sorted(
            nid for nid, n in self.topology_editor.model.nodes.items() if n["kind"] == "host"
        )
        self.traffic_panel.set_hosts(hosts)

    def _start_mininet(self, password: str) -> None:
        self.process_manager.cleanup_mininet_state(password=password)
        self.process_manager.start_mininet(SPEC_PATH, password=password)

    def _restart_mininet(self, password: str) -> None:
        self.process_manager.set_sudo_password(password)
        self.process_manager.restart_mininet(SPEC_PATH)

    def _start_everything(self) -> None:
        pw = self.control_panel.sudo_password()
        self.process_manager.set_sudo_password(pw)
        self._event_log.log("Starting controller...")
        self.process_manager.start_controller(SPEC_PATH)
        # Give the controller a couple seconds' head start so OVS switches
        # connect to a controller that's already listening — not strictly
        # required (OVS retries), but avoids a burst of connect failures
        # in the logs on a fresh start.
        QTimer.singleShot(2000, lambda: self._start_mininet(pw))

    def _stop_everything(self) -> None:
        self._event_log.log("Stopping network and controller...")
        self.process_manager.stop_mininet()
        self.process_manager.stop_controller()
        self.process_manager.cleanup_mininet_state()
        self._event_log.log("Stopped. Mininet/OVS state cleaned up.")

    def _apply_topology(self, spec_path: str) -> None:
        self._event_log.log(f"Applying new topology from {spec_path}...")
        self.process_manager.apply_topology(spec_path)
        self._refresh_traffic_hosts()
        self._event_log.log(
            "Topology applied — controller will pick up new link "
            "bandwidths on its next monitor tick without restarting."
        )

    def _on_crash(self, which: str, reason: str) -> None:
        self._event_log.log(f"⚠ {which} crashed: {reason}")
        QMessageBox.warning(self, f"{which} crashed", reason)

    # ── Shutdown ─────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        running = (
            self.process_manager.controller_status == ProcStatus.RUNNING
            or self.process_manager.mininet_status == ProcStatus.RUNNING
        )
        if running:
            reply = QMessageBox.question(
                self,
                "Stop and quit?",
                "The controller and/or network are still running. Stop them "
                "and clean up Mininet/OVS state before quitting?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
        self._event_log.log("Shutting down — terminating children and cleaning up...")
        self.traffic_manager.disconnect_from_server()
        self.process_manager.shutdown()
        super().closeEvent(event)


def main():
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

    win = OrchestratorWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
