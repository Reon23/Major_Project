"""
active_inference_dynamic.py  —  Topology-Independent Ryu SDN Controller
=========================================================================

This file contains only the RyuApp class.  All logic is delegated to:

  ai/belief.py            PathBelief
  ai/policy.py            compute_efe_for_path, select_best_path,
                          compute_multipath_weights
  sdn/constants.py        all constants
  sdn/topology_manager.py graph, switches, links, trunk ports, paths
  sdn/host_manager.py     host IP/MAC/location learning
  sdn/flow_manager.py     add/delete/install flow rules, PacketOut,
                          install_multipath_flows (SELECT groups)
  sdn/state_writer.py     atomic state.json export
  utils/ip_utils.py       IP address classification

Routing modes
-------------
SINGLE-PATH  (active path util < MULTIPATH_CONGESTION_THRESHOLD)
  EFE picks the single best path; plain output() actions are installed.

MULTIPATH    (active path util >= MULTIPATH_CONGESTION_THRESHOLD)
  All candidate paths are active simultaneously via an OF1.3 SELECT group.
  Bucket weights are proportional to each path's remaining headroom
  (congestion_threshold - belief.mu), computed by compute_multipath_weights().
  The switch hardware distributes packets per-flow (ECMP-style) without
  any controller involvement in the data plane.

Run
---
    ryu-manager --observe-links active_inference_dynamic.py

Verify
------
    mininet> pingall
    mininet> h1 ping h2
    mininet> iperf h1 h2
    mininet> iperf h3 h6
    mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
    mininet> sh ovs-ofctl -O OpenFlow13 dump-groups s1
"""

import threading
import time
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import arp, ethernet, ipv4, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event

from ai.belief import PathBelief
from ai.policy import compute_multipath_weights, compute_efe_for_path, select_best_path
from sdn import flow_manager as fm
from sdn.constants import (
    ARP_CACHE_TTL,
    ETH_TYPE_LLDP,
    LINK_FLAP_GRACE_SEC,
    MULTIPATH_CONGESTION_THRESHOLD,
    POLL_INTERVAL,
    PREFERRED_UTIL,
    STATE_JSON_PATH,
)
from sdn.host_manager import HostManager
from sdn.state_writer import write_state
from sdn.topology_manager import TopologyManager
from utils.ip_utils import is_valid_host_ip


class ActiveInferenceDynamic(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._lock = threading.Lock()

        # Managers
        self._topo = TopologyManager()
        self._hosts = HostManager()

        # Active flows
        # _flows[(src_ip, dst_ip)] = {
        #   "path":         [dpid, ...],     active single path (EFE best)
        #   "path_idx":     int,
        #   "beliefs":      {idx: PathBelief},
        #   "load_estimate": float,
        #   "multipath":    bool,            True when SELECT group is active
        # }
        self._flows = {}

        # Per-port byte / time accumulators
        self._last_bytes = defaultdict(int)  # (dpid, port_no) -> bytes
        self._last_time = {}  # (dpid, port_no) -> timestamp

        # Per-flow byte accumulators for direct load measurement
        # key: (src_ip, dst_ip) -> {"bytes": int, "time": float, "rate_mbps": float}
        self._flow_bytes = {}

        # ARP duplicate-suppression cache
        # key: (dpid, in_port, src_mac, dst_ip, opcode) -> expiry timestamp
        self._arp_seen = {}

        # Pending link removals, for flap debouncing.
        # key: (src_dpid, dst_dpid) normalised low->high -> hub.GreenThread
        self._pending_link_removal = {}

        # Monitor loop
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # =========================================================================
    #  OpenFlow: switch connects
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self._topo.add_switch(dp.id, dp)
        fm.install_table_miss(dp)
        self.logger.info("Switch s%d connected", dp.id)

    # =========================================================================
    #  Topology discovery events
    # =========================================================================

    @set_ev_cls(topo_event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        dpid = ev.switch.dp.id
        self._topo.add_switch(dpid, ev.switch.dp)
        self.logger.info("Topology: switch s%d discovered", dpid)

    @set_ev_cls(topo_event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        self._topo.remove_switch(dpid)
        self.logger.info("Topology: switch s%d left", dpid)

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        key = (min(src.dpid, dst.dpid), max(src.dpid, dst.dpid))

        # If a removal for this exact link is still pending (within the grace
        # window), this Add is the other half of a flap caused by a delayed
        # LLDP probe under congestion — cancel the pending removal and treat
        # the link as having never gone away. No flow flush, no topology edit.
        pending = self._pending_link_removal.pop(key, None)
        if pending is not None:
            hub.kill(pending)
            self.logger.info(
                "Topology: link s%d <-> s%d flap absorbed (re-add within %.1fs)",
                src.dpid,
                dst.dpid,
                LINK_FLAP_GRACE_SEC,
            )
            return

        self._topo.add_link(src.dpid, src.port_no, dst.dpid, dst.port_no)
        # Invalidate all flow candidates — topology changed
        with self._lock:
            self._flows.clear()
        self.logger.info(
            "Topology: link s%d-eth%d <-> s%d-eth%d",
            src.dpid,
            src.port_no,
            dst.dpid,
            dst.port_no,
        )

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_delete_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        key = (min(src.dpid, dst.dpid), max(src.dpid, dst.dpid))

        # Don't remove immediately. Schedule the removal after a short grace
        # period; if EventLinkAdd for the same link arrives first (the
        # common case under congestion, where the delete was spurious),
        # link_add_handler cancels this greenthread and nothing happens.
        greenthread = hub.spawn(
            self._apply_link_removal, src.dpid, src.port_no, dst.dpid, dst.port_no, key
        )
        self._pending_link_removal[key] = greenthread

    def _apply_link_removal(
        self, src_dpid: int, src_port: int, dst_dpid: int, dst_port: int, key: tuple
    ) -> None:
        hub.sleep(LINK_FLAP_GRACE_SEC)

        # Still pending after the grace period -> genuinely down. Apply it.
        with self._lock:
            still_pending = self._pending_link_removal.get(key) is not None
        if not still_pending:
            return  # was cancelled by a matching Add (shouldn't normally hit here)

        self._pending_link_removal.pop(key, None)
        self._topo.remove_link(src_dpid, src_port, dst_dpid, dst_port)
        with self._lock:
            stale = [
                k
                for k, v in self._flows.items()
                if TopologyManager.path_uses_link(v["path"], src_dpid, dst_dpid)
            ]
            for k in stale:
                del self._flows[k]
        self.logger.info(
            "Topology: link s%d <-> s%d removed (confirmed after %.1fs grace)",
            src_dpid,
            dst_dpid,
            LINK_FLAP_GRACE_SEC,
        )

    # =========================================================================
    #  Packet-in
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]

        # Keep datapath registry up to date
        self._topo.add_switch(dpid, dp)

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        # ── Drop LLDP (topology discovery) ───────────────────────────────────
        if eth.ethertype == ETH_TYPE_LLDP:
            return

        # ── Drop invalid source MACs ──────────────────────────────────────────
        if eth.src in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            return

        # ── ARP ───────────────────────────────────────────────────────────────
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self._handle_arp(dp, in_port, eth, arp_pkt, msg.data)
            return

        # ── IPv4 ──────────────────────────────────────────────────────────────
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            self._handle_ipv4(dp, in_port, eth, ip_pkt, msg.data)
            return

    # -------------------------------------------------------------------------
    #  ARP handler  (loop-safe, unicast when possible)
    # -------------------------------------------------------------------------

    def _handle_arp(self, dp, in_port, eth, arp_pkt, raw_data):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        src_ip = arp_pkt.src_ip
        dst_ip = arp_pkt.dst_ip
        src_mac = eth.src
        opcode = arp_pkt.opcode

        # Only learn hosts from edge (access) ports and valid IPs
        learned = self._hosts.learn_host(
            src_ip, src_mac, dpid, in_port, self._topo, self.logger
        )
        if learned:
            self._topo.invalidate_path_cache(src_ip)
            self._trigger_flows_for(src_ip)

        # ── ARP duplicate suppression ─────────────────────────────────────────
        now = time.time()
        dedup_key = (dpid, in_port, src_mac, dst_ip, opcode)
        with self._lock:
            expiry = self._arp_seen.get(dedup_key, 0)
        if now < expiry:
            return  # seen recently — drop to break potential loop
        with self._lock:
            self._arp_seen[dedup_key] = now + ARP_CACHE_TTL
            if len(self._arp_seen) > 2000:
                cutoff = now - ARP_CACHE_TTL
                self._arp_seen = {k: v for k, v in self._arp_seen.items() if v > cutoff}

        # ── Forward ARP ───────────────────────────────────────────────────────
        dst_loc = self._hosts.get_location(dst_ip)

        if dst_loc is not None:
            dst_dpid, dst_port = dst_loc
            if dst_dpid == dpid:
                if dst_port != in_port:
                    self.logger.debug(
                        "ARP unicast same-switch: %s -> %s out_port=%d",
                        src_ip,
                        dst_ip,
                        dst_port,
                    )
                    fm.packet_out(
                        dp, in_port, [parser.OFPActionOutput(dst_port)], raw_data
                    )
            else:
                out_port = self._topo.get_out_port_toward(dpid, dst_dpid)
                if out_port is not None and out_port != in_port:
                    self.logger.debug(
                        "ARP unicast toward s%d: %s -> %s out_port=%d",
                        dst_dpid,
                        src_ip,
                        dst_ip,
                        out_port,
                    )
                    fm.packet_out(
                        dp, in_port, [parser.OFPActionOutput(out_port)], raw_data
                    )
                else:
                    self._flood_safe(dp, in_port, raw_data)
        else:
            self.logger.debug(
                "ARP flood (dst unknown): %s -> %s from s%d-eth%d",
                src_ip,
                dst_ip,
                dpid,
                in_port,
            )
            self._flood_safe(dp, in_port, raw_data)

    # -------------------------------------------------------------------------
    #  IPv4 handler
    # -------------------------------------------------------------------------

    def _handle_ipv4(self, dp, in_port, eth, ip_pkt, raw_data):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst

        learned = self._hosts.learn_host(
            src_ip, eth.src, dpid, in_port, self._topo, self.logger
        )
        if learned:
            self._topo.invalidate_path_cache(src_ip)
            self._trigger_flows_for(src_ip)

        if self._hosts.is_known(dst_ip):
            if self._hosts.is_known(src_ip):
                self._install_flow_pair(src_ip, dst_ip)

            out_port = self._get_out_port(dpid, dst_ip)
            if out_port is not None:
                self.logger.debug(
                    "IPv4 PacketOut: %s -> %s s%d out_port=%d",
                    src_ip,
                    dst_ip,
                    dpid,
                    out_port,
                )
                fm.packet_out(dp, in_port, [parser.OFPActionOutput(out_port)], raw_data)
            else:
                self._flood_safe(dp, in_port, raw_data)
        else:
            self._flood_safe(dp, in_port, raw_data)

    # -------------------------------------------------------------------------
    #  Flood: safe, local to one switch only
    # -------------------------------------------------------------------------

    def _flood_safe(self, dp, in_port, raw_data):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        fm.packet_out(
            dp, in_port, [parser.OFPActionOutput(ofproto.OFPP_FLOOD)], raw_data
        )

    # =========================================================================
    #  Host learning callback
    # =========================================================================

    def _trigger_flows_for(self, new_ip: str) -> None:
        """Install flows for all known peers of new_ip."""
        for other_ip in self._hosts.all_ips():
            if other_ip == new_ip:
                continue
            self._install_flow_pair(new_ip, other_ip)
            self._install_flow_pair(other_ip, new_ip)

    # =========================================================================
    #  Flow installation — routing mode decision
    # =========================================================================

    def _install_flow_pair(self, src_ip: str, dst_ip: str) -> None:
        """
        Select path(s) via Active Inference and install bidirectional flow rules.

        Routing mode is decided per flow:
          * load_estimate < MULTIPATH_CONGESTION_THRESHOLD
              → single-path: EFE picks best path; plain output() actions.
          * load_estimate >= MULTIPATH_CONGESTION_THRESHOLD
              → multipath: ALL candidate paths active; SELECT group with
                headroom-proportional bucket weights.
        """
        fwd_candidates = self._topo.get_candidate_paths(src_ip, dst_ip, self._hosts)
        if not fwd_candidates:
            return

        rev_candidates = self._topo.get_candidate_paths(dst_ip, src_ip, self._hosts)
        if not rev_candidates:
            rev_candidates = [list(reversed(fwd_candidates[0]))]

        fwd_key = (src_ip, dst_ip)
        rev_key = (dst_ip, src_ip)

        # Initialise flow state if not present.
        # Seed load_estimate from live link utilisation on the first candidate
        # path so the routing mode decision is correct from the very first
        # install, rather than waiting for the EMA to climb from 0.0.
        with self._lock:
            for key, candidates in [
                (fwd_key, fwd_candidates),
                (rev_key, rev_candidates),
            ]:
                if key not in self._flows:
                    beliefs = {i: PathBelief() for i in range(len(candidates))}
                    seed_load = self._topo.path_max_util(candidates[0])
                    self._flows[key] = {
                        "path": candidates[0],
                        "path_idx": 0,
                        "beliefs": beliefs,
                        "load_estimate": seed_load,
                        "multipath": False,
                    }

        fwd_flow = self._flows[fwd_key]
        rev_flow = self._flows[rev_key]

        fwd_load = fwd_flow.get("load_estimate", 0.0)
        rev_load = rev_flow.get("load_estimate", 0.0)

        use_multipath = (
            len(fwd_candidates) > 1 and fwd_load >= MULTIPATH_CONGESTION_THRESHOLD
        )

        if use_multipath:
            fwd_weights = compute_multipath_weights(
                fwd_candidates, fwd_flow["beliefs"], fwd_load
            )
            rev_weights = compute_multipath_weights(
                rev_candidates, rev_flow["beliefs"], rev_load
            )
            fm.install_multipath_flows(
                src_ip,
                dst_ip,
                fwd_candidates,
                fwd_weights,
                rev_candidates,
                rev_weights,
                self._hosts,
                self._topo,
                logger=self.logger,
            )
            with self._lock:
                fwd_flow["multipath"] = True
                rev_flow["multipath"] = True
            self.logger.info(
                "Multipath activated: %s->%s  fwd_weights=%s",
                src_ip,
                dst_ip,
                fwd_weights,
            )
        else:
            # Single-path: EFE selection
            fwd_path = self._pick_path(fwd_key, fwd_candidates)
            rev_path = self._pick_path(rev_key, rev_candidates)
            fm.install_bidirectional_flows(
                src_ip,
                dst_ip,
                fwd_path,
                rev_path,
                self._hosts,
                self._topo,
                logger=self.logger,
            )
            with self._lock:
                fwd_flow["multipath"] = False
                rev_flow["multipath"] = False

    def _pick_path(self, flow_key: tuple, candidates: list) -> list:
        """
        Retrieve or initialise flow state, run EFE selection, return best path.
        """
        with self._lock:
            flow = self._flows.get(flow_key)
            if flow is None:
                beliefs = {i: PathBelief() for i in range(len(candidates))}
                flow = {
                    "path": candidates[0],
                    "path_idx": 0,
                    "beliefs": beliefs,
                    "load_estimate": 0.0,
                    "multipath": False,
                }
                self._flows[flow_key] = flow
                return candidates[0]

            beliefs = flow["beliefs"]
            active_idx = flow["path_idx"]
            # Resize if topology changed
            if len(beliefs) != len(candidates):
                new_beliefs = {
                    i: beliefs.get(i, PathBelief()) for i in range(len(candidates))
                }
                flow["beliefs"] = new_beliefs
                beliefs = new_beliefs

        best_idx = select_best_path(
            flow_key,
            candidates,
            beliefs,
            active_idx,
            flow.get("load_estimate", 0.0),
            logger=self.logger,
        )

        with self._lock:
            if best_idx != flow["path_idx"]:
                self.logger.info(
                    "Flow %s: rerouting path%d -> path%d",
                    flow_key,
                    flow["path_idx"],
                    best_idx,
                )
            flow["path_idx"] = best_idx
            flow["path"] = candidates[best_idx]

        return candidates[best_idx]

    def _get_out_port(self, dpid: int, dst_ip: str):
        """
        Return the outgoing port on switch `dpid` toward host `dst_ip`,
        using active flow table or direct host-port lookup.
        """
        dst_loc = self._hosts.get_location(dst_ip)
        if dst_loc is not None:
            dst_dpid, dst_port = dst_loc
            if dst_dpid == dpid:
                return dst_port

        with self._lock:
            for (s_ip, d_ip), flow_info in self._flows.items():
                if d_ip != dst_ip:
                    continue
                path = flow_info["path"]
                if dpid not in path:
                    continue
                idx = path.index(dpid)
                if idx + 1 < len(path):
                    return self._topo.get_out_port_between(dpid, path[idx + 1])
                if dst_loc is not None and dst_loc[0] == dpid:
                    return dst_loc[1]
        return None

    # =========================================================================
    #  Monitor loop — port stats + inference cycle
    # =========================================================================

    def _monitor_loop(self):
        from sdn.constants import (
            CONGESTION_INVALIDATE_THRESHOLD,
            FLOW_STATS_POLL_INTERVAL,
        )

        hub.sleep(5)  # wait for topology to stabilise
        _flow_stat_tick = 0

        while True:
            for dp in self._topo.all_datapaths():
                self._request_port_stats(dp)

            _flow_stat_tick += POLL_INTERVAL
            if _flow_stat_tick >= FLOW_STATS_POLL_INTERVAL:
                _flow_stat_tick = 0
                for dp in self._topo.all_datapaths():
                    self._request_flow_stats(dp)

            cleared = self._topo.invalidate_cache_for_congested_paths(
                CONGESTION_INVALIDATE_THRESHOLD
            )
            if cleared:
                self.logger.info(
                    "Path cache: cleared %d entries (congested links detected)", cleared
                )

            hub.sleep(POLL_INTERVAL)

    def _request_port_stats(self, dp):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY))

    def _request_flow_stats(self, dp):
        """Request per-flow byte counters from switch dp."""
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(
            parser.OFPFlowStatsRequest(
                dp,
                0,
                ofproto.OFPTT_ALL,
                ofproto.OFPP_ANY,
                ofproto.OFPG_ANY,
                match=parser.OFPMatch(),
            )
        )

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        """
        Receive per-flow byte counters and compute per-flow rate_mbps.
        """
        import time as _time

        now = _time.time()

        for stat in ev.msg.body:
            match = stat.match
            if match.get("eth_type") != 0x0800:
                continue
            src_ip = match.get("ipv4_src")
            dst_ip = match.get("ipv4_dst")
            if src_ip is None or dst_ip is None:
                continue

            key = (src_ip, dst_ip)
            total_bytes = stat.byte_count
            prev = self._flow_bytes.get(key)

            if prev is None:
                self._flow_bytes[key] = {
                    "bytes": total_bytes,
                    "time": now,
                    "rate_mbps": 0.0,
                }
                continue

            dt = max(now - prev["time"], 0.001)
            delta = max(0, total_bytes - prev["bytes"])
            rate_mbps = (delta * 8) / 1e6 / dt

            self._flow_bytes[key] = {
                "bytes": total_bytes,
                "time": now,
                "rate_mbps": rate_mbps,
            }

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        import time as _time

        dpid = ev.msg.datapath.id
        now = _time.time()

        sw_port_map = self._topo.get_sw_port_map()

        port_rates = {}
        for stat in ev.msg.body:
            port_no = stat.port_no
            if port_no >= 0xFFFFFFF0:
                continue
            key = (dpid, port_no)
            total_bytes = stat.rx_bytes + stat.tx_bytes
            last_b = self._last_bytes.get(key, total_bytes)
            last_t = self._last_time.get(key, now)
            dt = max(now - last_t, 0.001)
            delta_bytes = max(0, total_bytes - last_b)
            rate_mbps = (delta_bytes * 8) / 1e6 / dt
            self._last_bytes[key] = total_bytes
            self._last_time[key] = now
            port_rates[port_no] = rate_mbps

        for (src, dst), out_port in sw_port_map.items():
            if src != dpid:
                continue
            if out_port not in port_rates:
                continue
            rate_mbps = port_rates[out_port]
            existing = self._topo.get_link_util(src, dst)
            capacity = existing.get("capacity_mbps", 10.0)
            self._topo.update_link_util(src, dst, rate_mbps, capacity)

        self._run_inference_cycle()

    def _run_inference_cycle(self):
        """
        Update PathBeliefs for all flows, re-run EFE, reroute or rebalance
        multipath weights if beneficial, then export state.json.
        """
        events = []

        with self._lock:
            flow_keys = list(self._flows.keys())

        for flow_key in flow_keys:
            src_ip, dst_ip = flow_key
            candidates = self._topo.get_candidate_paths(src_ip, dst_ip, self._hosts)
            if not candidates:
                continue

            with self._lock:
                flow = self._flows.get(flow_key)
            if flow is None:
                continue

            # Topology can change between when this flow's path was chosen
            # and now (e.g. a link removal shrank the candidate set, or an
            # addition grew it). Reconcile beliefs/active_idx against the
            # freshly computed `candidates` before indexing into it, or a
            # stale path_idx / undersized beliefs dict will throw IndexError
            # / KeyError below.
            with self._lock:
                beliefs = flow["beliefs"]
                if len(beliefs) != len(candidates):
                    beliefs = {
                        i: beliefs.get(i, PathBelief()) for i in range(len(candidates))
                    }
                    flow["beliefs"] = beliefs

                active_idx = flow["path_idx"]
                if active_idx >= len(candidates):
                    active_idx = 0
                    flow["path_idx"] = 0
                    flow["path"] = candidates[0]

            # Update beliefs with measured utilisation
            for i, path in enumerate(candidates):
                util = self._topo.path_max_util(path)
                beliefs[i].update(util)

            for i in range(len(candidates)):
                if i == active_idx:
                    beliefs[i].reinforce_confidence()
                else:
                    beliefs[i].decay_confidence()

            # ── Direct per-flow load measurement ─────────────────────────────
            # Asymmetric EMA: fast rise (α=0.8) to detect congestion quickly;
            # slow fall (α=0.2) so we don't drop out of multipath the instant
            # a burst subsides.  Port-level bottleneck util is used as a floor
            # so we never under-estimate during the gap before flow stats arrive.
            measured = self._flow_bytes.get(flow_key)
            port_floor = self._topo.path_max_util(candidates[active_idx])
            if measured and measured["rate_mbps"] > 0:
                active_capacity = self._topo.get_link_util(
                    candidates[active_idx][0],
                    candidates[active_idx][1]
                    if len(candidates[active_idx]) > 1
                    else candidates[active_idx][0],
                ).get("capacity_mbps", 10.0)
                raw_load = min(measured["rate_mbps"] / max(active_capacity, 0.001), 1.0)
            else:
                raw_load = port_floor
            prev_load = flow.get("load_estimate", 0.0)
            alpha = 0.8 if raw_load > prev_load else 0.2
            load_est = max(alpha * raw_load + (1.0 - alpha) * prev_load, port_floor)
            with self._lock:
                flow["load_estimate"] = load_est

            # ── Routing mode decision ─────────────────────────────────────────
            use_multipath = (
                len(candidates) > 1 and load_est >= MULTIPATH_CONGESTION_THRESHOLD
            )

            if use_multipath:
                # Recompute weights and refresh SELECT groups
                rev_candidates = self._topo.get_candidate_paths(
                    dst_ip, src_ip, self._hosts
                ) or [list(reversed(candidates[active_idx]))]

                with self._lock:
                    rev_flow = self._flows.get((dst_ip, src_ip))
                rev_load = rev_flow.get("load_estimate", 0.0) if rev_flow else 0.0
                rev_beliefs = (
                    rev_flow["beliefs"]
                    if rev_flow
                    else {i: PathBelief() for i in range(len(rev_candidates))}
                )

                fwd_weights = compute_multipath_weights(candidates, beliefs, load_est)
                rev_weights = compute_multipath_weights(
                    rev_candidates, rev_beliefs, rev_load
                )

                fm.install_multipath_flows(
                    src_ip,
                    dst_ip,
                    candidates,
                    fwd_weights,
                    rev_candidates,
                    rev_weights,
                    self._hosts,
                    self._topo,
                    logger=self.logger,
                )
                with self._lock:
                    flow["multipath"] = True
                    if rev_flow:
                        rev_flow["multipath"] = True

                events.append(
                    f"MULTIPATH {src_ip}->{dst_ip}: "
                    f"load={load_est:.2f} weights={fwd_weights}"
                )

            else:
                # Single-path EFE
                best_idx = select_best_path(
                    flow_key,
                    candidates,
                    beliefs,
                    active_idx,
                    load_est,
                    logger=self.logger,
                )

                if best_idx != active_idx:
                    with self._lock:
                        flow["path_idx"] = best_idx
                        flow["path"] = candidates[best_idx]
                        flow["multipath"] = False

                    rev_candidates = self._topo.get_candidate_paths(
                        dst_ip, src_ip, self._hosts
                    ) or [list(reversed(candidates[best_idx]))]
                    rev_path = self._pick_path((dst_ip, src_ip), rev_candidates)

                    fm.install_bidirectional_flows(
                        src_ip,
                        dst_ip,
                        candidates[best_idx],
                        rev_path,
                        self._hosts,
                        self._topo,
                        logger=self.logger,
                    )
                    events.append(
                        f"REROUTED {src_ip}->{dst_ip}: "
                        f"path{active_idx}->path{best_idx}"
                    )
                else:
                    with self._lock:
                        flow["multipath"] = False
                    events.append(
                        f"Held {src_ip}->{dst_ip} on path{active_idx} "
                        f"(util={beliefs[active_idx].mu:.2f})"
                    )

        event_str = " | ".join(events) if events else "Monitoring..."

        with self._lock:
            flows_snapshot = dict(self._flows)

        write_state(
            self._topo,
            self._hosts,
            flows_snapshot,
            event=event_str,
            path=STATE_JSON_PATH,
        )
