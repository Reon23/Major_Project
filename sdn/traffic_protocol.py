"""
sdn/traffic_protocol.py — shared constants for the traffic-generation RPC
between the orchestrator GUI (traffic_manager.py, a QTcpSocket client) and
the Mininet child process (topology.py's FlowRPCServer).

Protocol: newline-delimited JSON, one object per line, in both directions.

Client -> server commands:
    {"cmd": "start_flow", "id": "<client-chosen id>", "src": "h1", "dst": "h2",
     "proto": "tcp"|"udp", "bandwidth_mbps": 10.0, "duration_s": 30}
    {"cmd": "stop_flow", "id": "..."}
    {"cmd": "stop_all"}
    {"cmd": "list_flows"}

Server -> client:
    {"type": "response", "cmd": "start_flow", "id": "...", "ok": true|false, "error": "..."}
    {"type": "response", "cmd": "list_flows", "ok": true, "flows": [...]}
    {"type": "event", "event": "flow_stats", "id": "...", "mbps": 93.4, "interval": "2.00-3.00"}
    {"type": "event", "event": "flow_log", "id": "...", "line": "..."}
    {"type": "event", "event": "flow_finished", "id": "...", "reason": "completed"|"stopped"|"error"}

Why a socket instead of the Mininet CLI's stdin/stdout: Mininet host
namespaces (and the `net.get(host).popen(...)` API needed to run iperf3
inside them) only exist inside the Mininet child process. Driving this
from CLI text commands would mean scraping unstructured CLI output for
concurrent flows, which is fragile. A small JSON-line socket gives the GUI
a structured, concurrent-safe way to start/stop/monitor multiple flows.
"""

TRAFFIC_RPC_HOST = "127.0.0.1"
TRAFFIC_RPC_PORT = 5566

# Iperf3 server ports are auto-assigned starting here, incrementing per
# flow (never reused within a run) so concurrent flows never collide even
# when they share a destination host.
IPERF_BASE_PORT = 5201
