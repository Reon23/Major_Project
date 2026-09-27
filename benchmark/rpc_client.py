"""
benchmark/rpc_client.py — JSON-line TCP client for the FlowRPCServer.

The server lives inside the Mininet child process (topology.py's
FlowRPCServer) and accepts newline-delimited JSON commands on
TRAFFIC_RPC_HOST:PORT. See sdn/traffic_protocol.py for the wire format.

This client is intentionally synchronous: a benchmark scenario issues
a small number of commands and waits for each response before
proceeding — there's no need for full-duplex async I/O. The trickier
piece is that the server also *pushes* events (flow_stats, flow_log,
flow_finished) without prompting; we collect those into a queue that
the TrialRecorder drains.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from sdn.traffic_protocol import TRAFFIC_RPC_HOST, TRAFFIC_RPC_PORT

_log = logging.getLogger(__name__)


class FlowRPCError(RuntimeError):
    """Raised when the FlowRPCServer returns ok=false or fails to respond."""


class FlowRPCClient:
    """
    Synchronous JSON-line client for the FlowRPCServer.

    Usage
    -----
        client = FlowRPCClient()
        client.connect()
        client.start_flow({"id": "f1", "src": "h1", "dst": "h2", ...})
        events = client.drain_events(timeout=0.1)
        client.stop_all()
        client.close()

    The client runs a background thread that reads newline-delimited JSON
    from the socket, splits responses (matched to outstanding commands
    by FIFO order, since the server processes one command at a time)
    from unsolicited events (flow_stats / flow_log / flow_finished),
    and queues the latter for the recorder to drain.
    """

    CONNECT_TIMEOUT = 5.0
    CMD_TIMEOUT = 10.0  # generous; start_flow waits for iperf3 server bind

    def __init__(
        self,
        host: str = TRAFFIC_RPC_HOST,
        port: int = TRAFFIC_RPC_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()

        # Outstanding command responses are matched in FIFO order: the
        # server processes one command at a time and replies in order.
        self._pending_responses: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        # Unsolicited events go here for the recorder to drain.
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def connect(self, timeout: float = CONNECT_TIMEOUT) -> None:
        """Open the socket and start the reader thread."""
        if self._sock is not None:
            return
        deadline = time.time() + timeout
        last_exc: Optional[Exception] = None
        while time.time() < deadline:
            try:
                sock = socket.create_connection(
                    (self._host, self._port), timeout=2.0
                )
                sock.settimeout(None)  # blocking, reader thread handles it
                self._sock = sock
                break
            except (ConnectionError, OSError) as exc:
                last_exc = exc
                time.sleep(0.5)
        else:
            raise FlowRPCError(
                f"Could not connect to FlowRPCServer at "
                f"{self._host}:{self._port} within {timeout}s: {last_exc}"
            )

        self._stop_reader.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="FlowRPCClient-reader", daemon=True
        )
        self._reader_thread.start()

    def close(self) -> None:
        """Stop the reader thread and close the socket."""
        self._stop_reader.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._reader_thread = None

    # ── Public API ─────────────────────────────────────────────────────

    def start_flow(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a start_flow command. Returns the server response dict."""
        return self._send_command({"cmd": "start_flow", **params})

    def stop_flow(self, flow_id: str) -> Dict[str, Any]:
        return self._send_command({"cmd": "stop_flow", "id": flow_id})

    def stop_all(self) -> Dict[str, Any]:
        return self._send_command({"cmd": "stop_all"})

    def list_flows(self) -> List[Dict[str, Any]]:
        resp = self._send_command({"cmd": "list_flows"})
        if not resp.get("ok"):
            raise FlowRPCError(f"list_flows failed: {resp.get('error', '?')}")
        return resp.get("flows", []) or []

    def drain_events(self, timeout: float = 0.0) -> List[Dict[str, Any]]:
        """Drain queued events (flow_stats, flow_log, flow_finished)."""
        out: List[Dict[str, Any]] = []
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0 and not out:
                # If we got nothing and the timeout elapsed, do one final
                # non-blocking check before giving up.
                try:
                    out.append(self._events.get_nowait())
                except queue.Empty:
                    break
                continue
            try:
                out.append(self._events.get(timeout=max(0.0, remaining)))
            except queue.Empty:
                break
        return out

    # ── Internals ──────────────────────────────────────────────────────

    def _send_command(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        if self._sock is None:
            raise FlowRPCError("not connected — call connect() first")
        line = (json.dumps(msg) + "\n").encode("utf-8")
        try:
            self._sock.sendall(line)
        except OSError as exc:
            raise FlowRPCError(f"send failed: {exc}") from exc
        try:
            resp = self._pending_responses.get(timeout=self.CMD_TIMEOUT)
        except queue.Empty as exc:
            raise FlowRPCError(
                f"no response within {self.CMD_TIMEOUT}s for cmd={msg.get('cmd')}"
            ) from exc
        return resp

    def _reader_loop(self) -> None:
        # Buffer for partial lines (the server sends one JSON object per
        # newline-terminated line, but TCP gives us byte chunks).
        buf = b""
        while not self._stop_reader.is_set():
            try:
                if self._sock is None:
                    break
                chunk = self._sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break  # server closed
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    _log.debug("rpc_client: bad JSON line: %r (%s)", line, exc)
                    continue
                self._dispatch_obj(obj)

    def _dispatch_obj(self, obj: Dict[str, Any]) -> None:
        """Route an incoming object to either the pending-response queue
        or the events queue."""
        msg_type = obj.get("type")
        if msg_type == "response":
            self._pending_responses.put(obj)
        elif msg_type == "event":
            self._events.put(obj)
        else:
            # Unknown — log and queue as event so it's not lost.
            _log.debug("rpc_client: unknown msg type=%r: %r", msg_type, obj)
            self._events.put(obj)


# ── Convenience: probe server reachability ─────────────────────────────────


def server_reachable(
    host: str = TRAFFIC_RPC_HOST,
    port: int = TRAFFIC_RPC_PORT,
    timeout: float = 1.0,
) -> bool:
    """Quick TCP probe — useful for the runner to wait for Mininet start."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
