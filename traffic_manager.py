"""
traffic_manager.py — GUI-side client for the traffic RPC server embedded
in the Mininet child process (topology.py's FlowRPCServer). See
sdn/traffic_protocol.py for the wire format.

Uses QTcpSocket (async, integrates with the Qt event loop — no separate
polling thread needed) rather than a blocking socket.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket, QTcpSocket

from sdn.traffic_protocol import TRAFFIC_RPC_HOST, TRAFFIC_RPC_PORT


class TrafficManager(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    connection_error = pyqtSignal(str)

    # id, ok, error message (error message empty on success)
    flow_ack = pyqtSignal(str, bool, str)
    # id, mbps, interval label
    flow_stats = pyqtSignal(str, float, str)
    # id, raw iperf3 output line
    flow_log = pyqtSignal(str, str)
    # id, reason ("completed" | "stopped" | "error")
    flow_finished = pyqtSignal(str, str)
    flows_listed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = QTcpSocket(self)
        self.socket.readyRead.connect(self._on_ready_read)
        self.socket.connected.connect(lambda: self.connected.emit())
        self.socket.disconnected.connect(lambda: self.disconnected.emit())
        self.socket.errorOccurred.connect(self._on_socket_error)
        self._buffer = b""

    # ── connection lifecycle ─────────────────────────────────────────────

    def connect_to_server(
        self, host: str = TRAFFIC_RPC_HOST, port: int = TRAFFIC_RPC_PORT
    ) -> None:
        if self.socket.state() in (
            QAbstractSocket.SocketState.ConnectedState,
            QAbstractSocket.SocketState.ConnectingState,
        ):
            return
        self._buffer = b""
        self.socket.connectToHost(host, port)

    def disconnect_from_server(self) -> None:
        if self.socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self.socket.disconnectFromHost()

    def is_connected(self) -> bool:
        return self.socket.state() == QAbstractSocket.SocketState.ConnectedState

    def _on_socket_error(self, _err) -> None:
        # Expected/noisy while Mininet is still starting up (nothing
        # listening yet) — surfaced via connection_error, not raised.
        self.connection_error.emit(self.socket.errorString())

    # ── commands ─────────────────────────────────────────────────────────

    def _send(self, obj: dict) -> None:
        if not self.is_connected():
            self.connection_error.emit(
                "Not connected to the Mininet traffic server yet."
            )
            return
        self.socket.write((json.dumps(obj) + "\n").encode())

    def start_flow(
        self,
        src: str,
        dst: str,
        proto: str,
        bandwidth_mbps: Optional[float],
        duration_s: int,
        flow_id: Optional[str] = None,
    ) -> str:
        fid = flow_id or uuid.uuid4().hex[:8]
        self._send(
            {
                "cmd": "start_flow",
                "id": fid,
                "src": src,
                "dst": dst,
                "proto": proto,
                "bandwidth_mbps": bandwidth_mbps,
                "duration_s": duration_s,
            }
        )
        return fid

    def stop_flow(self, flow_id: str) -> None:
        self._send({"cmd": "stop_flow", "id": flow_id})

    def stop_all(self) -> None:
        self._send({"cmd": "stop_all"})

    def request_list(self) -> None:
        self._send({"cmd": "list_flows"})

    # ── incoming data ────────────────────────────────────────────────────

    def _on_ready_read(self) -> None:
        self._buffer += bytes(self.socket.readAll())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "response":
            cmd = msg.get("cmd")
            if cmd == "start_flow":
                self.flow_ack.emit(
                    msg.get("id", ""), bool(msg.get("ok")), msg.get("error", "")
                )
            elif cmd == "list_flows" and msg.get("ok"):
                self.flows_listed.emit(msg.get("flows", []))
            elif not msg.get("ok"):
                self.connection_error.emit(msg.get("error", "unknown error"))
        elif kind == "event":
            event = msg.get("event")
            if event == "flow_stats":
                self.flow_stats.emit(
                    msg.get("id", ""), float(msg.get("mbps", 0.0)), msg.get("interval", "")
                )
            elif event == "flow_log":
                self.flow_log.emit(msg.get("id", ""), msg.get("line", ""))
            elif event == "flow_finished":
                self.flow_finished.emit(msg.get("id", ""), msg.get("reason", "completed"))
