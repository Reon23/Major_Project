"""
process_manager.py — QProcess-based lifecycle management for the two child
processes the orchestrator replaces manual terminals for:

  1. The Ryu controller:   ryu-manager --observe-links active_inference_dynamic.py
  2. The Mininet network:  topology.py --spec <topology_spec.json>  (needs root)

Design notes (see task doc §3)
-------------------------------
- Uses QProcess (not raw subprocess) so stdout/stderr streaming integrates
  with the Qt event loop instead of needing a separate polling thread.
- The GUI itself runs as a normal user. Only the Mininet child is
  escalated, via `sudo -S` with the password piped to the process's stdin
  from a one-time password prompt (see app.py's PasswordDialog). If the
  environment has a passwordless-sudo rule scoped to this launcher script,
  an empty password still works (sudo -S reads a blank line and, with
  NOPASSWD configured, never actually needs it).
- "Apply topology" while a network is already running = stop_mininet()
  (graceful CLI exit, then terminate if needed) followed by an
  unconditional `sudo -S mn -c` cleanup, then start_mininet() with the new
  spec. The controller is never restarted for a topology change — it is
  topology-independent (LLDP-driven discovery); see task doc §3.
- Clean shutdown (app close or explicit Stop) always runs the `mn -c`
  equivalent so the user never needs a terminal to clean up orphaned
  OVS/Mininet state before the next run.
"""

from __future__ import annotations

import os
import shlex
from enum import Enum
from typing import Optional

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal


class ProcStatus(Enum):
    NOT_STARTED = "not started"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"


# Mininet CLI prints this prompt once the network is fully up; we treat its
# first appearance as the "running" transition (best-effort — the status
# also flips to RUNNING as soon as the process has been alive for a beat
# and hasn't exited, so a differently-themed Mininet build doesn't wedge
# the status indicator forever).
MININET_READY_MARKER = "mininet>"


class ProcessManager(QObject):
    controller_status_changed = pyqtSignal(ProcStatus)
    mininet_status_changed = pyqtSignal(ProcStatus)

    controller_log = pyqtSignal(str)
    mininet_log = pyqtSignal(str)
    cleanup_log = pyqtSignal(str)

    # Emitted with a human-readable reason when a child dies unexpectedly
    # (i.e. not as part of a requested stop/restart).
    controller_crashed = pyqtSignal(str)
    mininet_crashed = pyqtSignal(str)

    # Emitted when starting Mininet needs a sudo password and none / an
    # incorrect one has been supplied yet. app.py connects this to a
    # password-prompt dialog and calls start_mininet(..., password=...) or
    # set_sudo_password() again.
    sudo_password_rejected = pyqtSignal()

    def __init__(
        self,
        project_dir: str,
        python_bin: str = "python3",
        ryu_manager_bin: str = "ryu-manager",
        parent=None,
    ):
        super().__init__(parent)
        self._project_dir = project_dir
        self._python_bin = python_bin
        self._ryu_manager_bin = ryu_manager_bin

        self._controller_proc: Optional[QProcess] = None
        self._mininet_proc: Optional[QProcess] = None
        self._cleanup_proc: Optional[QProcess] = None

        self._controller_status = ProcStatus.NOT_STARTED
        self._mininet_status = ProcStatus.NOT_STARTED

        self._controller_stop_requested = False
        self._mininet_stop_requested = False

        self._sudo_password: Optional[str] = None
        self._mininet_seen_ready = False

    # ── Public status ────────────────────────────────────────────────────

    @property
    def controller_status(self) -> ProcStatus:
        return self._controller_status

    @property
    def mininet_status(self) -> ProcStatus:
        return self._mininet_status

    def set_sudo_password(self, password: str) -> None:
        """Cache the sudo password for this session (memory only — never
        written to disk or logged)."""
        self._sudo_password = password

    # ── Controller lifecycle ────────────────────────────────────────────

    def start_controller(self, spec_path: str) -> None:
        if self._controller_proc is not None:
            return  # already running/starting

        self._set_controller_status(ProcStatus.STARTING)
        self._controller_stop_requested = False

        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("SDN_TOPOLOGY_SPEC", spec_path)
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(self._project_dir)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

        proc.readyReadStandardOutput.connect(
            lambda: self._on_output(proc, self.controller_log, "controller")
        )
        proc.started.connect(lambda: self._set_controller_status(ProcStatus.RUNNING))
        proc.finished.connect(self._on_controller_finished)
        proc.errorOccurred.connect(
            lambda err: self._on_process_error("controller", err)
        )

        proc.start(
            self._ryu_manager_bin,
            ["--observe-links", "active_inference_dynamic.py"],
        )
        self._controller_proc = proc

    def stop_controller(self) -> None:
        if self._controller_proc is None:
            return
        self._controller_stop_requested = True
        self._set_controller_status(ProcStatus.STOPPING)
        self._controller_proc.terminate()
        if not self._controller_proc.waitForFinished(3000):
            self._controller_proc.kill()
            self._controller_proc.waitForFinished(2000)

    def restart_controller(self, spec_path: str) -> None:
        self.stop_controller()
        self.start_controller(spec_path)

    def _on_controller_finished(self, exit_code: int, exit_status) -> None:
        was_requested = self._controller_stop_requested
        self._controller_proc = None
        self._controller_stop_requested = False
        if was_requested or exit_code == 0:
            self._set_controller_status(ProcStatus.NOT_STARTED)
        else:
            self._set_controller_status(ProcStatus.CRASHED)
            self.controller_crashed.emit(
                f"ryu-manager exited unexpectedly (code {exit_code}). "
                f"Check the Logs console for the traceback."
            )

    # ── Mininet lifecycle ────────────────────────────────────────────────

    def start_mininet(self, spec_path: str, password: Optional[str] = None) -> None:
        if self._mininet_proc is not None:
            return

        if password is not None:
            self._sudo_password = password

        self._set_mininet_status(ProcStatus.STARTING)
        self._mininet_stop_requested = False
        self._mininet_seen_ready = False

        proc = QProcess(self)
        proc.setWorkingDirectory(self._project_dir)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

        proc.readyReadStandardOutput.connect(
            lambda: self._on_mininet_output(proc)
        )
        proc.finished.connect(self._on_mininet_finished)
        proc.errorOccurred.connect(lambda err: self._on_process_error("mininet", err))

        cmd = [
            "-S",  # read password from stdin
            "-p", "",  # no prompt text (we drive stdin ourselves)
            self._python_bin,
            "topology.py",
            "--spec",
            spec_path,
        ]
        proc.start("sudo", cmd)
        proc.started.connect(lambda: self._feed_sudo_password(proc))
        self._mininet_proc = proc

    def _feed_sudo_password(self, proc: QProcess) -> None:
        pw = self._sudo_password or ""
        proc.write((pw + "\n").encode())

    def stop_mininet(self) -> None:
        """
        Graceful stop: send 'exit' to the Mininet CLI over stdin so
        net.stop() runs cleanly inside the child; fall back to terminate/
        kill if it doesn't exit in time. Callers should follow this with
        cleanup_mininet_state() to guarantee no orphaned OVS bridges/ports
        linger (the mn -c equivalent) — apply_topology()/shutdown() below
        already do this.
        """
        if self._mininet_proc is None:
            return
        self._mininet_stop_requested = True
        self._set_mininet_status(ProcStatus.STOPPING)
        try:
            self._mininet_proc.write(b"exit\n")
        except RuntimeError:
            pass  # process object already gone
        if not self._mininet_proc.waitForFinished(6000):
            self._mininet_proc.terminate()
            if not self._mininet_proc.waitForFinished(3000):
                self._mininet_proc.kill()
                self._mininet_proc.waitForFinished(2000)

    def cleanup_mininet_state(self, password: Optional[str] = None) -> None:
        """
        Run the equivalent of `sudo mn -c`: clears leftover OVS bridges,
        veth pairs, and namespaces from a previous run. Safe to call even
        when nothing is running. Synchronous (blocks briefly) because it's
        only ever called around start/stop/apply/shutdown transitions,
        never on a hot path.
        """
        if password is not None:
            self._sudo_password = password

        proc = QProcess(self)
        proc.setWorkingDirectory(self._project_dir)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self._on_output(proc, self.cleanup_log, "cleanup")
        )
        proc.start("sudo", ["-S", "-p", "", "mn", "-c"])
        proc.started.connect(lambda: self._feed_sudo_password(proc))
        proc.waitForFinished(15000)
        self._cleanup_proc = None  # released after synchronous wait

    def restart_mininet(self, spec_path: str) -> None:
        self.stop_mininet()
        self.cleanup_mininet_state()
        self.start_mininet(spec_path)

    def apply_topology(self, spec_path: str) -> None:
        """
        Tear down the current Mininet network and relaunch it with a new
        spec (task doc §3 "Apply topology"). The controller is left
        running throughout — it re-discovers the new graph via LLDP and
        (via ActiveInferenceDynamic._reload_topology_spec) picks up the
        new link bandwidths on its next monitor tick without a restart.
        """
        was_running = self._mininet_proc is not None
        if was_running:
            self.stop_mininet()
        self.cleanup_mininet_state()
        self.start_mininet(spec_path)

    def _on_mininet_output(self, proc: QProcess) -> None:
        raw = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        if not raw:
            return
        if not self._mininet_seen_ready and MININET_READY_MARKER in raw:
            self._mininet_seen_ready = True
            self._set_mininet_status(ProcStatus.RUNNING)
        for line in raw.splitlines():
            if line:
                self.mininet_log.emit(line)

    def _on_mininet_finished(self, exit_code: int, exit_status) -> None:
        was_requested = self._mininet_stop_requested
        self._mininet_proc = None
        self._mininet_stop_requested = False
        if was_requested or exit_code == 0:
            self._set_mininet_status(ProcStatus.NOT_STARTED)
        else:
            self._set_mininet_status(ProcStatus.CRASHED)
            reason = (
                "sudo authentication failed or was cancelled — check the "
                "sudo password and try again."
                if exit_code in (1, 2)
                else f"Mininet exited unexpectedly (code {exit_code}). "
                f"Check the Logs console for the traceback."
            )
            self.mininet_crashed.emit(reason)
            if exit_code in (1,):
                self.sudo_password_rejected.emit()

    # ── Shared helpers ───────────────────────────────────────────────────

    def _on_output(self, proc: QProcess, signal: pyqtSignal, tag: str) -> None:
        raw = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        for line in raw.splitlines():
            if line:
                signal.emit(line)

    def _on_process_error(self, which: str, error) -> None:
        msg = f"{which}: process error ({error})"
        if which == "controller":
            self.controller_log.emit(msg)
        else:
            self.mininet_log.emit(msg)

    def _set_controller_status(self, status: ProcStatus) -> None:
        self._controller_status = status
        self.controller_status_changed.emit(status)

    def _set_mininet_status(self, status: ProcStatus) -> None:
        self._mininet_status = status
        self.mininet_status_changed.emit(status)

    # ── Shutdown ─────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """
        Terminate both children and guarantee no orphaned Mininet/OVS
        state on disk — call from the main window's closeEvent. Blocks
        briefly (a few seconds worst case) which is acceptable for an
        app-close path.
        """
        self.stop_controller()
        self.stop_mininet()
        self.cleanup_mininet_state()
