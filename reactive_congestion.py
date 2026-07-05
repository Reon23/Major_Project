r"""
reactive_congestion.py — Reactive Congestion-Aware Ryu SDN Controller
=======================================================================

PURPOSE
-------
This is a *reactive* baseline controller: it does NOT predict congestion
or trade AI models between controllers (that's what your friend's
active_inference_dynamic.py does). Instead it:

    1. Learns the topology via LLDP  (run with --observe-links)
    2. Learns hosts via ARP / IPv4 (like a normal learning switch)
    3. Installs the shortest inter-switch path for every new flow
    4. Polls OpenFlow port statistics every POLL_INTERVAL seconds
    5. Computes utilisation on every switch<->switch link
    6. When a link's utilisation crosses CONGESTION_THRESHOLD, it is
       marked "congested". Every active flow whose current path uses
       that link is immediately rerouted onto an alternate path that
       avoids all currently-congested links (if one exists).
    7. When utilisation drops back under RECOVERY_THRESHOLD the link is
       un-marked (hysteresis prevents flapping).

This is a "detect-then-fix" (reactive) strategy, as opposed to a
"predict-then-avoid" (proactive) strategy used by active_inference_dynamic.py.

VISUALISATION PARITY WITH active_inference_dynamic.py
-------------------------------------------------------
Like the AI controller, this file now exports a JSON state snapshot every
monitor tick (STATE_JSON_PATH, default "reactive_state.json") so the same
dynamic_visualizer.py can render this controller side-by-side with the
active-inference one for comparison.

Each link and each active flow is tagged with a traffic-light `status` /
`color`:

    green  (#2ecc71)  status="normal"     util < WARNING_THRESHOLD
    orange (#f39c12)  status="warning"    WARNING_THRESHOLD <= util < CONGESTION_THRESHOLD
    red    (#e74c3c)  status="congested"  util >= CONGESTION_THRESHOLD

A flow's color is the worst color of any link on its *currently installed*
path, so the visualizer can paint packets/edges red or orange as soon as a
flow starts crossing a hot link — even before a reroute actually happens
(orange = "still routed here, but getting close"; red = "actively
congested, reroute should already be underway").

NOTE: exact field names below are my best-effort match to what
active_inference_dynamic.py / state_writer.py expose (topology, hosts,
flows, event string). If your dynamic_visualizer.py expects different key
names, share sdn/state_writer.py and I'll line this up exactly.

TOPOLOGY ASSUMPTIONS (matches topology.py — change the constants below
if you change topology.py)
------------------------------------------------------------------------
    Switches : s1, s2, s3, s4   (OpenFlow 1.3)
    Hosts    : h1..h6
    Inter-switch (bottleneck) links : 10 Mbps  -> s1-s2, s1-s3, s2-s4, s3-s4
    Redundant paths h1(s1) -> h2(s4):
        Path A: s1 -> s2 -> s4
        Path B: s1 -> s3 -> s4

RUN
---
    Terminal 1:
        ryu-manager --observe-links reactive_congestion.py

    Terminal 2:
        sudo mn -c
        sudo python3 topology.py

    Terminal 3 (optional, same visualizer as the AI controller):
        python dynamic_visualizer.py --state reactive_state.json

    Inside mininet CLI, generate congestion to trigger a reroute, e.g.:
        mininet> h1 iperf -s &
        mininet> h2 iperf -c h1 -t 30 -b 9M &      (saturate s1<->s2 link)
        mininet> h1 ping h2                         (watch it get rerouted)

Watch the ryu-manager terminal — you will see log lines like:

    [WARNING]    link s1<->s2 utilisation=58.2% (>= 55% orange threshold)
    [CONGESTION] link s1<->s2 utilisation=87.3% (threshold 70%, RED)
    [REROUTE]    10.0.0.1 -> 10.0.0.2 : [1, 2, 4] -> [1, 3, 4]
    [RECOVERED]  link s1<->s2 utilisation=31.0% back under 40%
"""

import json
import os
import tempfile
import time
from collections import defaultdict, deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import arp, ethernet, ipv4, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event

# =============================================================================
# Tunable constants — edit these if you change topology.py
# =============================================================================

INTER_SWITCH_BW_MBPS = 10.0   # matches the 10 Mbps bottleneck links in topology.py
CONGESTION_THRESHOLD = 0.70   # mark a link congested (RED) at 70% of its capacity
WARNING_THRESHOLD = 0.55      # mark a link "getting hot" (ORANGE) at 55% of capacity
RECOVERY_THRESHOLD = 0.40     # un-mark congestion once utilisation drops below 40% (hysteresis)
POLL_INTERVAL = 2             # seconds between port-stats polls
ARP_CACHE_TTL = 2             # seconds — suppress duplicate ARP floods (loop safety)
ETH_TYPE_LLDP = 0x88CC
FLOW_PRIORITY = 10

STATE_JSON_PATH = "reactive_state.json"

# ---- traffic-light colors, shared vocabulary with active_inference_dynamic.py ----
STATUS_NORMAL = "normal"
STATUS_WARNING = "warning"
STATUS_CONGESTED = "congested"

COLOR_BY_STATUS = {
    STATUS_NORMAL: "#2ecc71",     # green
    STATUS_WARNING: "#f39c12",    # orange
    STATUS_CONGESTED: "#e74c3c",  # red
}

_STATUS_RANK = {STATUS_NORMAL: 0, STATUS_WARNING: 1, STATUS_CONGESTED: 2}


def status_for_util(util: float) -> str:
    """Map a link/path utilisation fraction (0..1) to a traffic-light status."""
    if util >= CONGESTION_THRESHOLD:
        return STATUS_CONGESTED
    if util >= WARNING_THRESHOLD:
        return STATUS_WARNING
    return STATUS_NORMAL


def worst_status(statuses) -> str:
    """Return the most severe status among an iterable of statuses."""
    worst = STATUS_NORMAL
    for s in statuses:
        if _STATUS_RANK.get(s, 0) > _STATUS_RANK.get(worst, 0):
            worst = s
    return worst


class ReactiveCongestionController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ---- switch / datapath bookkeeping -------------------------------
        self.datapaths = {}                 # dpid -> Datapath object

        # ---- topology graph ------------------------------------------------
        # graph[dpid][neighbor_dpid] = out_port_on_dpid_facing_neighbor
        self.graph = defaultdict(dict)
        # port_to_neighbor[dpid][port] = neighbor_dpid   (reverse lookup)
        self.port_to_neighbor = defaultdict(dict)

        # ---- host learning ---------------------------------------------
        self.host_location = {}             # ip -> (dpid, port)
        self.host_mac = {}                  # ip -> mac
        self.mac_to_port = defaultdict(dict)  # dpid -> {mac: port}   (plain L2 fallback)

        # ---- active flows we are managing --------------------------------
        # (src_ip, dst_ip) -> {"path": [dpid,...]}
        self.active_flows = {}

        # ---- congestion state (functional — drives rerouting) -------------
        self.congested_links = set()        # set of frozenset({dpid_a, dpid_b}), hysteresis-gated
        self._last_port_bytes = {}          # (dpid, port) -> (bytes, timestamp)
        self._link_util = {}                # frozenset({dpid_a,dpid_b}) -> utilisation (0..1)

        # ---- congestion state (display — drives visualizer colors) --------
        # frozenset({dpid_a,dpid_b}) -> "normal" | "warning" | "congested"
        self._link_status = {}

        # ---- ARP loop-safety -------------------------------------------
        self._arp_seen = {}                 # (dpid,in_port,mac,dst_ip,opcode) -> expiry

        # ---- rolling event log, mirrors active_inference_dynamic.py's event_str ----
        self._events = deque(maxlen=20)

        self.monitor_thread = hub.spawn(self._monitor_loop)

    # =========================================================================
    # Switch connect / table-miss
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self._install_table_miss(dp)
        self.logger.info("Switch s%d connected", dp.id)

    def _install_table_miss(self, dp):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, actions)

    def _add_flow(self, dp, priority, match, actions, idle_timeout=0, hard_timeout=0):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )
        dp.send_msg(mod)

    def _del_flow(self, dp, match):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        mod = parser.OFPFlowMod(
            datapath=dp,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match,
        )
        dp.send_msg(mod)

    # =========================================================================
    # Topology discovery (requires: ryu-manager --observe-links reactive_congestion.py)
    # =========================================================================

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        src, dst = ev.link.src, ev.link.dst
        self.graph[src.dpid][dst.dpid] = src.port_no
        self.graph[dst.dpid][src.dpid] = dst.port_no
        self.port_to_neighbor[src.dpid][src.port_no] = dst.dpid
        self.port_to_neighbor[dst.dpid][dst.port_no] = src.dpid
        self._link_status.setdefault(frozenset({src.dpid, dst.dpid}), STATUS_NORMAL)
        self.logger.info("Topology: link s%d <-> s%d", src.dpid, dst.dpid)

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_delete_handler(self, ev):
        src, dst = ev.link.src, ev.link.dst
        self.graph[src.dpid].pop(dst.dpid, None)
        self.graph[dst.dpid].pop(src.dpid, None)
        self.port_to_neighbor[src.dpid].pop(src.port_no, None)
        self.port_to_neighbor[dst.dpid].pop(dst.port_no, None)
        edge = frozenset({src.dpid, dst.dpid})
        self.congested_links.discard(edge)
        self._link_status.pop(edge, None)
        self.logger.info("Topology: link s%d <-> s%d removed", src.dpid, dst.dpid)

    # =========================================================================
    # Packet-in : ARP + IPv4 learning / forwarding
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ETH_TYPE_LLDP:
            return
        if eth.src in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            return

        # plain L2 learning table (fallback for anything not IP)
        self.mac_to_port[dpid][eth.src] = in_port

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self._handle_arp(dp, in_port, eth, arp_pkt, msg.data)
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            self._handle_ipv4(dp, in_port, eth, ip_pkt, msg.data)
            return

        # unknown protocol -> flood locally
        self._flood(dp, in_port, msg.data)

    def _learn_host(self, ip, mac, dpid, port):
        if ip in ("0.0.0.0",):
            return
        is_new = self.host_location.get(ip) != (dpid, port)
        self.host_location[ip] = (dpid, port)
        self.host_mac[ip] = mac
        if is_new:
            self.logger.info("Host learned: %s at s%d-eth%d", ip, dpid, port)

    def _handle_arp(self, dp, in_port, eth, arp_pkt, raw_data):
        dpid = dp.id
        # only learn from access ports (a port that is NOT a known inter-switch port)
        if in_port not in self.port_to_neighbor.get(dpid, {}):
            self._learn_host(arp_pkt.src_ip, eth.src, dpid, in_port)

        # loop-safety: suppress duplicate ARP floods (topology has physical loops)
        now = time.time()
        key = (dpid, in_port, eth.src, arp_pkt.dst_ip, arp_pkt.opcode)
        if now < self._arp_seen.get(key, 0):
            return
        self._arp_seen[key] = now + ARP_CACHE_TTL

        dst_loc = self.host_location.get(arp_pkt.dst_ip)
        if dst_loc is not None:
            dst_dpid, dst_port = dst_loc
            if dst_dpid == dpid:
                self._packet_out(dp, in_port, dst_port, raw_data)
                return
            out_port = self.graph.get(dpid, {}).get(
                self._next_hop_toward(dpid, dst_dpid)
            )
            if out_port is not None:
                self._packet_out(dp, in_port, out_port, raw_data)
                return
        # unknown destination -> flood
        self._flood(dp, in_port, raw_data)

    def _handle_ipv4(self, dp, in_port, eth, ip_pkt, raw_data):
        dpid = dp.id
        src_ip, dst_ip = ip_pkt.src, ip_pkt.dst

        if in_port not in self.port_to_neighbor.get(dpid, {}):
            self._learn_host(src_ip, eth.src, dpid, in_port)

        if src_ip in self.host_location and dst_ip in self.host_location:
            self._ensure_flow_installed(src_ip, dst_ip)
            self._ensure_flow_installed(dst_ip, src_ip)

        out_port = self._get_out_port(dpid, dst_ip)
        if out_port is not None:
            self._packet_out(dp, in_port, out_port, raw_data)
        else:
            self._flood(dp, in_port, raw_data)

    def _get_out_port(self, dpid, dst_ip):
        loc = self.host_location.get(dst_ip)
        if loc and loc[0] == dpid:
            return loc[1]
        for (s_ip, d_ip), info in self.active_flows.items():
            if d_ip != dst_ip:
                continue
            path = info["path"]
            if dpid not in path:
                continue
            idx = path.index(dpid)
            if idx + 1 < len(path):
                return self.graph.get(dpid, {}).get(path[idx + 1])
            elif loc:
                return loc[1]
        return None

    def _packet_out(self, dp, in_port, out_port, raw_data):
        parser = dp.ofproto_parser
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=dp.ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=raw_data,
        )
        dp.send_msg(out)

    def _flood(self, dp, in_port, raw_data):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=raw_data,
        )
        dp.send_msg(out)

    # =========================================================================
    # Path computation (BFS, shortest hop-count, optionally avoiding edges)
    # =========================================================================

    def _bfs_path(self, src_dpid, dst_dpid, avoid_edges):
        if src_dpid == dst_dpid:
            return [src_dpid]
        visited = {src_dpid}
        queue = deque([[src_dpid]])
        while queue:
            path = queue.popleft()
            node = path[-1]
            for neighbor in self.graph.get(node, {}):
                edge = frozenset({node, neighbor})
                if edge in avoid_edges or neighbor in visited:
                    continue
                new_path = path + [neighbor]
                if neighbor == dst_dpid:
                    return new_path
                visited.add(neighbor)
                queue.append(new_path)
        return None

    def _compute_path(self, src_ip, dst_ip, avoid_edges=None):
        avoid_edges = avoid_edges or set()
        src_loc = self.host_location.get(src_ip)
        dst_loc = self.host_location.get(dst_ip)
        if src_loc is None or dst_loc is None:
            return None
        src_dpid, dst_dpid = src_loc[0], dst_loc[0]
        path = self._bfs_path(src_dpid, dst_dpid, avoid_edges)
        if path is None and avoid_edges:
            # no congestion-free path exists — fall back to best-effort path
            path = self._bfs_path(src_dpid, dst_dpid, set())
        return path

    def _next_hop_toward(self, dpid, dst_dpid):
        path = self._bfs_path(dpid, dst_dpid, set())
        if path and len(path) > 1:
            return path[1]
        return None

    # =========================================================================
    # Flow install / reroute
    # =========================================================================

    def _ensure_flow_installed(self, src_ip, dst_ip):
        key = (src_ip, dst_ip)
        if key in self.active_flows:
            return
        path = self._compute_path(src_ip, dst_ip, self.congested_links)
        if not path:
            return
        self.active_flows[key] = {"path": path}
        self._install_path(src_ip, dst_ip, path)
        self.logger.info("Flow installed: %s -> %s via %s", src_ip, dst_ip, path)

    def _install_path(self, src_ip, dst_ip, path):
        dst_loc = self.host_location.get(dst_ip)
        if dst_loc is None:
            return
        for idx, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue
            if idx + 1 < len(path):
                out_port = self.graph.get(dpid, {}).get(path[idx + 1])
            else:
                out_port = dst_loc[1]
            if out_port is None:
                continue
            parser = dp.ofproto_parser
            match = parser.OFPMatch(eth_type=0x0800, ipv4_src=src_ip, ipv4_dst=dst_ip)
            actions = [parser.OFPActionOutput(out_port)]
            self._add_flow(dp, FLOW_PRIORITY, match, actions)

    def _uninstall_path(self, src_ip, dst_ip, path):
        for dpid in path:
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue
            parser = dp.ofproto_parser
            match = parser.OFPMatch(eth_type=0x0800, ipv4_src=src_ip, ipv4_dst=dst_ip)
            self._del_flow(dp, match)

    def _reroute_affected_flows(self):
        """Re-route every active flow whose current path crosses a congested link."""
        for key, info in list(self.active_flows.items()):
            src_ip, dst_ip = key
            path = info["path"]
            path_edges = {frozenset({path[i], path[i + 1]}) for i in range(len(path) - 1)}
            if not (path_edges & self.congested_links):
                continue  # this flow is unaffected

            new_path = self._compute_path(src_ip, dst_ip, self.congested_links)
            if not new_path or new_path == path:
                continue  # no better alternative available

            self.logger.info(
                "[REROUTE] %s -> %s : %s -> %s",
                src_ip, dst_ip, path, new_path,
            )
            self._events.append(f"REROUTED {src_ip}->{dst_ip}: {path} -> {new_path}")
            self._uninstall_path(src_ip, dst_ip, path)
            self._install_path(src_ip, dst_ip, new_path)
            info["path"] = new_path

    # =========================================================================
    # Monitoring loop — port stats polling + congestion detection
    # =========================================================================

    def _monitor_loop(self):
        hub.sleep(5)  # let topology discovery settle first
        while True:
            for dp in list(self.datapaths.values()):
                self._request_port_stats(dp)
            hub.sleep(POLL_INTERVAL)

    def _request_port_stats(self, dp):
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        reroute_needed = False

        for stat in ev.msg.body:
            port_no = stat.port_no
            if port_no >= 0xFFFFFFF0:
                continue
            # only care about inter-switch (bottleneck) ports
            neighbor = self.port_to_neighbor.get(dpid, {}).get(port_no)
            if neighbor is None:
                continue

            key = (dpid, port_no)
            total_bytes = stat.rx_bytes + stat.tx_bytes
            prev = self._last_port_bytes.get(key)
            self._last_port_bytes[key] = (total_bytes, now)
            if prev is None:
                continue

            prev_bytes, prev_time = prev
            dt = max(now - prev_time, 0.001)
            delta_bytes = max(0, total_bytes - prev_bytes)
            mbps = (delta_bytes * 8) / 1e6 / dt
            utilisation = mbps / INTER_SWITCH_BW_MBPS

            edge = frozenset({dpid, neighbor})
            self._link_util[edge] = utilisation

            # ---- functional hysteresis: drives actual rerouting ------------
            if utilisation >= CONGESTION_THRESHOLD and edge not in self.congested_links:
                self.congested_links.add(edge)
                self.logger.warning(
                    "[CONGESTION] link s%d<->s%d utilisation=%.1f%% (threshold %.0f%%, RED)",
                    dpid, neighbor, utilisation * 100, CONGESTION_THRESHOLD * 100,
                )
                self._events.append(
                    f"CONGESTED s{min(dpid,neighbor)}<->s{max(dpid,neighbor)} "
                    f"({utilisation * 100:.0f}%)"
                )
                reroute_needed = True
            elif utilisation < RECOVERY_THRESHOLD and edge in self.congested_links:
                self.congested_links.discard(edge)
                self.logger.info(
                    "[RECOVERED] link s%d<->s%d utilisation=%.1f%% back under %.0f%%",
                    dpid, neighbor, utilisation * 100, RECOVERY_THRESHOLD * 100,
                )
                self._events.append(
                    f"RECOVERED s{min(dpid,neighbor)}<->s{max(dpid,neighbor)} "
                    f"({utilisation * 100:.0f}%)"
                )

            # ---- display-only tri-color status: drives visualizer ----------
            new_status = status_for_util(utilisation)
            old_status = self._link_status.get(edge, STATUS_NORMAL)
            if new_status != old_status:
                if new_status == STATUS_WARNING and old_status == STATUS_NORMAL:
                    self.logger.warning(
                        "[WARNING] link s%d<->s%d utilisation=%.1f%% "
                        "(>= %.0f%% orange threshold)",
                        dpid, neighbor, utilisation * 100, WARNING_THRESHOLD * 100,
                    )
                self._link_status[edge] = new_status

        if reroute_needed:
            self._reroute_affected_flows()

        self._write_state()

    # =========================================================================
    # State export — JSON snapshot for dynamic_visualizer.py
    # =========================================================================

    def _flow_status(self, path):
        """Worst-case status among all edges of `path`, for flow/packet coloring."""
        edge_statuses = [
            self._link_status.get(frozenset({path[i], path[i + 1]}), STATUS_NORMAL)
            for i in range(len(path) - 1)
        ]
        return worst_status(edge_statuses) if edge_statuses else STATUS_NORMAL

    def _write_state(self):
        """
        Atomically export a JSON snapshot of switches, links (with
        utilisation + traffic-light color), hosts, and active flows
        (with their current path + traffic-light color), plus a rolling
        event log — mirrors the shape active_inference_dynamic.py exports
        via state_writer.write_state(), so the same visualizer can point
        at either controller.
        """
        switches = sorted(self.datapaths.keys())

        links = []
        seen_edges = set()
        for dpid, neighbors in self.port_to_neighbor.items():
            for port_no, neighbor in neighbors.items():
                edge = frozenset({dpid, neighbor})
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                a, b = sorted(edge)
                util = self._link_util.get(edge, 0.0)
                status = self._link_status.get(edge, status_for_util(util))
                links.append(
                    {
                        "src": a,
                        "dst": b,
                        "utilization": round(util, 4),
                        "status": status,
                        "color": COLOR_BY_STATUS[status],
                        "congested": edge in self.congested_links,
                    }
                )

        hosts = {
            ip: {
                "mac": self.host_mac.get(ip),
                "dpid": loc[0],
                "port": loc[1],
            }
            for ip, loc in self.host_location.items()
        }

        flows = {}
        for (src_ip, dst_ip), info in self.active_flows.items():
            path = info["path"]
            status = self._flow_status(path)
            flows[f"{src_ip}->{dst_ip}"] = {
                "path": path,
                "status": status,
                "color": COLOR_BY_STATUS[status],
            }

        state = {
            "timestamp": time.time(),
            "controller": "reactive",
            "mode": "reactive-congestion-aware",
            "thresholds": {
                "warning": WARNING_THRESHOLD,
                "congestion": CONGESTION_THRESHOLD,
                "recovery": RECOVERY_THRESHOLD,
            },
            "switches": switches,
            "links": links,
            "hosts": hosts,
            "flows": flows,
            "event": self._events[-1] if self._events else "Monitoring...",
            "events": list(self._events),
        }

        try:
            dir_name = os.path.dirname(os.path.abspath(STATE_JSON_PATH)) or "."
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".reactive_state_", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, STATE_JSON_PATH)
        except Exception as exc:  # pragma: no cover — defensive, never crash the controller
            self.logger.debug("state.json write failed: %s", exc)
