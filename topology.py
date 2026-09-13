r"""
topology.py — Spec-driven Mininet Topology for Active Inference SDN Controller
================================================================================

Builds a Mininet topology from a JSON topology spec (see
sdn/topology_spec.py for the schema) instead of a hardcoded class. This lets
the orchestrator GUI's topology editor apply arbitrary topologies without
touching this file. Running this script directly (with no --spec) still
gives the default 6-host/4-switch topology, so the manual three-terminal
workflow keeps working exactly as before.

Controller: RemoteController at the spec's controller.ip:controller.port
(default 127.0.0.1:6633).

IMPORTANT — start the Ryu controller BEFORE running this script:
    Terminal 1:
        ryu-manager --observe-links active_inference_dynamic.py

    Terminal 2:
        sudo mn -c
        sudo python3 topology.py                  # default topology
        sudo python3 topology.py --spec my.json    # custom topology

    Inside Mininet CLI:
        mininet> pingall
        mininet> h1 ping h2
        mininet> iperf h1 h2
        mininet> iperf h3 h6
"""

import argparse
import json
import re
import signal
import socket
import socketserver
import subprocess
import threading
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

from sdn.topology_spec import default_spec, load_spec, validate_spec
from sdn.traffic_protocol import IPERF_BASE_PORT, TRAFFIC_RPC_HOST, TRAFFIC_RPC_PORT

# Matches iperf3's per-interval report lines, e.g.:
#   [  5]   2.00-3.00   sec  11.8 MBytes  98.9 Mbits/sec
_BW_LINE_RE = re.compile(
    r"([\d.]+)-([\d.]+)\s+sec\s+[\d.]+\s+\wBytes\s+([\d.]+)\s+([KMG])bits/sec"
)
_UNIT_TO_MBPS = {"K": 1e-3, "M": 1.0, "G": 1e3}


class SpecTopo(Topo):
    """
    Generic topology built entirely from a topology spec dict — no
    hardcoded node names. See sdn/topology_spec.py for the schema.
    """

    def build(self, spec=None, **_opts):
        spec = spec or default_spec()
        validate_spec(spec)  # raises TopologySpecError on anything fatal

        # -- Switches (OpenFlow 1.3) -------------------------------------------
        # dpid is left to Mininet's default (derived from the trailing
        # number in the switch name, e.g. "s3" -> dpid 3) so it matches the
        # sN <-> dpid==N convention assumed throughout sdn/ and state.json.
        mn_switches = {}
        for sw in spec.get("switches", []):
            sid = sw["id"]
            mn_switches[sid] = self.addSwitch(sid, protocols="OpenFlow13")

        # -- Hosts ---------------------------------------------------------------
        mn_hosts = {}
        for host in spec.get("hosts", []):
            hid = host["id"]
            mn_hosts[hid] = self.addHost(hid, ip=host["ip"])

        # -- Links -----------------------------------------------------------------
        nodes = {**mn_switches, **mn_hosts}
        for link in spec.get("links", []):
            src, dst = nodes[link["src"]], nodes[link["dst"]]
            link_kwargs = {"cls": TCLink, "bw": link["bw"]}
            if "delay" in link:
                link_kwargs["delay"] = link["delay"]
            if "max_queue_size" in link:
                link_kwargs["max_queue_size"] = link["max_queue_size"]
                link_kwargs["use_htb"] = True
            self.addLink(src, dst, **link_kwargs)


class FlowController:
    """
    Owns the lifecycle of iperf3-based traffic flows inside this Mininet
    process (the only place `net.get(host).popen(...)` — i.e. "run this
    command inside that host's network namespace" — is possible). Thread
    safe: mutated from per-connection handler threads and read from a
    background reaper thread.
    """

    def __init__(self, net, broadcast):
        self.net = net
        self._broadcast = broadcast  # callable(dict) -> sent to all GUI connections
        self._lock = threading.Lock()
        self._flows = {}  # id -> flow dict
        self._next_port = IPERF_BASE_PORT
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    def _alloc_port(self) -> int:
        with self._lock:
            port = self._next_port
            self._next_port += 1
            return port

    def start_flow(self, params: dict) -> dict:
        fid = params.get("id")
        src, dst = params.get("src"), params.get("dst")
        proto = (params.get("proto") or "udp").lower()
        bw = params.get("bandwidth_mbps")
        duration = int(params.get("duration_s") or 10)

        if not fid:
            return {"ok": False, "error": "missing flow id"}
        with self._lock:
            if fid in self._flows:
                return {"ok": False, "error": f"flow id {fid!r} already exists"}

        src_node = self.net.get(src) if src in self.net else None
        dst_node = self.net.get(dst) if dst in self.net else None
        if src_node is None or dst_node is None:
            return {"ok": False, "error": f"unknown host(s): {src!r}, {dst!r}"}
        if src == dst:
            return {"ok": False, "error": "src and dst must be different hosts"}

        port = self._alloc_port()
        dst_ip = dst_node.IP()

        # -1 : server exits after serving one connection — self-cleaning
        # for the common case; the reaper below still force-kills it if
        # the flow is stopped early or the client never connects.
        server_proc = dst_node.popen(
            ["iperf3", "-s", "-p", str(port), "-1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)  # let the server bind before the client connects

        client_cmd = ["iperf3", "-c", dst_ip, "-p", str(port), "-t", str(duration), "-i", "1"]
        if proto == "udp":
            client_cmd += ["-u", "-b", f"{bw or 10}M"]
        elif bw:
            client_cmd += ["-b", f"{bw}M"]

        client_proc = src_node.popen(
            client_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )

        with self._lock:
            self._flows[fid] = {
                "id": fid,
                "src": src,
                "dst": dst,
                "proto": proto,
                "bandwidth_mbps": bw,
                "duration_s": duration,
                "port": port,
                "server_proc": server_proc,
                "client_proc": client_proc,
                "status": "running",
                "stop_requested": False,
                "started_at": time.time(),
            }

        threading.Thread(target=self._reader_loop, args=(fid,), daemon=True).start()
        return {"ok": True}

    def _reader_loop(self, fid: str) -> None:
        flow = self._flows.get(fid)
        if flow is None:
            return
        proc = flow["client_proc"]
        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip()
                if not line:
                    continue
                self._broadcast(
                    {"type": "event", "event": "flow_log", "id": fid, "line": line}
                )
                m = _BW_LINE_RE.search(line)
                if m:
                    start, end, value, unit = m.groups()
                    mbps = float(value) * _UNIT_TO_MBPS.get(unit, 1.0)
                    self._broadcast(
                        {
                            "type": "event",
                            "event": "flow_stats",
                            "id": fid,
                            "mbps": round(mbps, 2),
                            "interval": f"{start}-{end}",
                        }
                    )
        except (ValueError, OSError):
            pass  # process/pipe torn down from underneath us — reaper handles cleanup

    def _reap_loop(self) -> None:
        while True:
            time.sleep(0.5)
            with self._lock:
                items = list(self._flows.items())
            for fid, flow in items:
                if flow["status"] != "running":
                    continue
                if flow["client_proc"].poll() is not None:
                    self._finish_flow(fid, "stopped" if flow["stop_requested"] else "completed")

    def _finish_flow(self, fid: str, reason: str) -> None:
        with self._lock:
            flow = self._flows.get(fid)
            if flow is None or flow["status"] != "running":
                return
            flow["status"] = reason
            server_proc = flow["server_proc"]
            client_proc = flow["client_proc"]
        for proc in (client_proc, server_proc):
            try:
                if proc.poll() is None:
                    proc.kill()
            except OSError:
                pass
        self._broadcast(
            {"type": "event", "event": "flow_finished", "id": fid, "reason": reason}
        )

    def stop_flow(self, fid: str) -> dict:
        with self._lock:
            flow = self._flows.get(fid)
            if flow is None:
                return {"ok": False, "error": f"no such flow {fid!r}"}
            if flow["status"] != "running":
                return {"ok": True}  # already finished/stopped
            flow["stop_requested"] = True
        self._finish_flow(fid, "stopped")
        return {"ok": True}

    def stop_all(self) -> dict:
        with self._lock:
            ids = [fid for fid, f in self._flows.items() if f["status"] == "running"]
        for fid in ids:
            self.stop_flow(fid)
        return {"ok": True}

    def list_flows(self) -> dict:
        with self._lock:
            out = [
                {
                    "id": f["id"],
                    "src": f["src"],
                    "dst": f["dst"],
                    "proto": f["proto"],
                    "bandwidth_mbps": f["bandwidth_mbps"],
                    "duration_s": f["duration_s"],
                    "status": f["status"],
                    "elapsed_s": round(time.time() - f["started_at"], 1),
                }
                for f in self._flows.values()
            ]
        return {"ok": True, "flows": out}


class _FlowRPCHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "FlowRPCServer" = self.server.flow_rpc_server  # type: ignore[attr-defined]
        server.add_client(self.request)
        try:
            for raw_line in self.rfile:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                response = server.dispatch(msg)
                if response is not None:
                    self._write({"type": "response", "cmd": msg.get("cmd"), **response})
        except (OSError, ConnectionError):
            pass
        finally:
            server.remove_client(self.request)

    def _write(self, obj: dict) -> None:
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode())
        except (OSError, BrokenPipeError):
            pass


class FlowRPCServer:
    """
    Threaded TCP JSON-line server, embedded in the Mininet process, that
    lets the GUI start/stop/monitor iperf3 traffic flows between hosts.
    See sdn/traffic_protocol.py for the wire format.
    """

    def __init__(self, net, host: str = TRAFFIC_RPC_HOST, port: int = TRAFFIC_RPC_PORT):
        self.controller = FlowController(net, self._broadcast)
        self._clients = set()
        self._clients_lock = threading.Lock()

        class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server((host, port), _FlowRPCHandler)
        self._server.flow_rpc_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.controller.stop_all()
        self._server.shutdown()
        self._server.server_close()

    def add_client(self, sock: socket.socket) -> None:
        with self._clients_lock:
            self._clients.add(sock)

    def remove_client(self, sock: socket.socket) -> None:
        with self._clients_lock:
            self._clients.discard(sock)

    def _broadcast(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        with self._clients_lock:
            dead = []
            for sock in self._clients:
                try:
                    sock.sendall(data)
                except OSError:
                    dead.append(sock)
            for sock in dead:
                self._clients.discard(sock)

    def dispatch(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "start_flow":
            return self.controller.start_flow(msg)
        if cmd == "stop_flow":
            return self.controller.stop_flow(msg.get("id"))
        if cmd == "stop_all":
            return self.controller.stop_all()
        if cmd == "list_flows":
            return self.controller.list_flows()
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def run(spec: dict):
    print("\n*** Make sure the Ryu controller is already running:")
    print("***   ryu-manager --observe-links active_inference_dynamic.py\n")

    topo = SpecTopo(spec=spec)
    net = Mininet(
        topo=topo,
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
    )

    ctrl_cfg = spec.get("controller", {"ip": "127.0.0.1", "port": 6633})
    net.addController(
        "c0",
        controller=RemoteController,
        ip=ctrl_cfg.get("ip", "127.0.0.1"),
        port=ctrl_cfg.get("port", 6633),
    )

    net.start()

    # Give OVS switches time to connect to the controller and for
    # Ryu topology discovery (LLDP) to finish mapping all links.
    print("\n*** Waiting 5 s for controller and topology discovery to settle...")
    time.sleep(5)

    print("\n*** Hosts:", [h.name for h in net.hosts])
    print("*** Switches:", [s.name for s in net.switches])

    # Traffic-generation RPC server (task: "send traffic between hosts
    # from the Control Panel"). Started before the CLI so the GUI can
    # start driving traffic as soon as it sees the "mininet>" prompt.
    flow_server = FlowRPCServer(net)
    flow_server.start()
    print(
        f"\n*** Traffic RPC server listening on "
        f"{TRAFFIC_RPC_HOST}:{TRAFFIC_RPC_PORT} (used by the orchestrator's "
        f"Traffic Panel)"
    )

    # Make sure a SIGTERM (e.g. QProcess::terminate() from the GUI, or a
    # plain `kill`) still runs the cleanup below instead of dropping
    # straight through — CLI(net) would otherwise never return.
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    print("\n*** Entering CLI — type 'exit' to quit")
    print("*** Suggested tests:")
    print("***   pingall")
    print("***   iperf <h1> <h2>\n")
    try:
        CLI(net)
    except KeyboardInterrupt:
        pass
    finally:
        flow_server.stop()
        net.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Spec-driven Mininet topology for the Active Inference SDN demo"
    )
    parser.add_argument(
        "--spec",
        default=None,
        help=(
            "Path to a topology spec JSON file (see sdn/topology_spec.py). "
            "If omitted, the built-in default 6-host/4-switch topology is used."
        ),
    )
    args = parser.parse_args()

    if args.spec:
        spec = load_spec(args.spec)
    else:
        spec = default_spec()

    setLogLevel("info")
    run(spec)


if __name__ == "__main__":
    main()
