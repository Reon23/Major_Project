"""
active_inference_dynamic.py  —  Topology-Independent Ryu SDN Controller
=========================================================================

This file contains only the RyuApp class.  All logic is delegated to:

  ai/belief.py            PathBelief
  ai/policy.py            compute_efe_for_path, select_best_path
  sdn/constants.py        all constants
  sdn/topology_manager.py graph, switches, links, trunk ports, paths
  sdn/host_manager.py     host IP/MAC/location learning
  sdn/flow_manager.py     add/delete/install flow rules, PacketOut
  sdn/state_writer.py     atomic state.json export
  utils/ip_utils.py       IP address classification

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
from ai.policy import compute_efe_for_path, select_best_path
from sdn import flow_manager as fm
from sdn.constants import (
    ARP_CACHE_TTL,
    ETH_TYPE_LLDP,
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
        #   "path": [dpid, ...],
        #   "path_idx": int,
        #   "beliefs": {idx: PathBelief},
        #   "load_estimate": float,
        # }
        self._flows = {}

        # Per-port byte / time accumulators
        self._last_bytes = defaultdict(int)   # (dpid, port_no) -> bytes
        self._last_time = {}                  # (dpid, port_no) -> timestamp

        # ARP duplicate-suppression cache
        # key: (dpid, in_port, src_mac, dst_ip, opcode) -> expiry timestamp
        self._arp_seen = {}

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
        self._topo.add_link(src.dpid, src.port_no, dst.dpid, dst.port_no)
        # Invalidate all flow candidates — topology changed
        with self._lock:
            self._flows.clear()
        self.logger.info(
            "Topology: link s%d-eth%d <-> s%d-eth%d",
            src.dpid, src.port_no, dst.dpid, dst.port_no,
        )

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_delete_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        self._topo.remove_link(src.dpid, src.port_no, dst.dpid, dst.port_no)
        with self._lock:
            stale = [
                k for k, v in self._flows.items()
                if TopologyManager.path_uses_link(v["path"], src.dpid, dst.dpid)
            ]
            for k in stale:
                del self._flows[k]
        self.logger.info("Topology: link s%d <-> s%d removed", src.dpid, dst.dpid)

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
            return   # seen recently — drop to break potential loop
        with self._lock:
            self._arp_seen[dedup_key] = now + ARP_CACHE_TTL
            if len(self._arp_seen) > 2000:
                cutoff = now - ARP_CACHE_TTL
                self._arp_seen = {
                    k: v for k, v in self._arp_seen.items() if v > cutoff
                }

        # ── Forward ARP ───────────────────────────────────────────────────────
        dst_loc = self._hosts.get_location(dst_ip)

        if dst_loc is not None:
            dst_dpid, dst_port = dst_loc
            if dst_dpid == dpid:
                # Destination host is on this same switch
                if dst_port != in_port:
                    self.logger.debug(
                        "ARP unicast same-switch: %s -> %s out_port=%d",
                        src_ip, dst_ip, dst_port,
                    )
                    fm.packet_out(dp, in_port,
                                  [parser.OFPActionOutput(dst_port)], raw_data)
            else:
                # Forward toward the switch hosting dst_ip
                out_port = self._topo.get_out_port_toward(dpid, dst_dpid)
                if out_port is not None and out_port != in_port:
                    self.logger.debug(
                        "ARP unicast toward s%d: %s -> %s out_port=%d",
                        dst_dpid, src_ip, dst_ip, out_port,
                    )
                    fm.packet_out(dp, in_port,
                                  [parser.OFPActionOutput(out_port)], raw_data)
                else:
                    self._flood_safe(dp, in_port, raw_data)
        else:
            # Destination unknown — controlled flood (dedup prevents storm)
            self.logger.debug(
                "ARP flood (dst unknown): %s -> %s from s%d-eth%d",
                src_ip, dst_ip, dpid, in_port,
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

        # Only learn from access ports and valid IPs
        learned = self._hosts.learn_host(
            src_ip, eth.src, dpid, in_port, self._topo, self.logger
        )
        if learned:
            self._topo.invalidate_path_cache(src_ip)
            self._trigger_flows_for(src_ip)

        # ── Forward or install flows ──────────────────────────────────────────
        if self._hosts.is_known(dst_ip):
            if self._hosts.is_known(src_ip):
                self._install_flow_pair(src_ip, dst_ip)

            out_port = self._get_out_port(dpid, dst_ip)
            if out_port is not None:
                self.logger.debug(
                    "IPv4 PacketOut: %s -> %s s%d out_port=%d",
                    src_ip, dst_ip, dpid, out_port,
                )
                fm.packet_out(dp, in_port,
                              [parser.OFPActionOutput(out_port)], raw_data)
            else:
                self._flood_safe(dp, in_port, raw_data)
        else:
            # Destination unknown
            self._flood_safe(dp, in_port, raw_data)

    # -------------------------------------------------------------------------
    #  Flood: safe, local to one switch only
    # -------------------------------------------------------------------------

    def _flood_safe(self, dp, in_port, raw_data):
        """
        Flood out of all ports except in_port using OFPP_FLOOD.
        OVS's OFPP_FLOOD respects STP and never sends back on in_port,
        so it is safe to use here as long as the ARP dedup cache
        prevents the controller from re-processing the same broadcast.
        """
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        fm.packet_out(dp, in_port,
                      [parser.OFPActionOutput(ofproto.OFPP_FLOOD)], raw_data)

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
    #  Flow installation
    # =========================================================================

    def _install_flow_pair(self, src_ip: str, dst_ip: str) -> None:
        """
        Select path via Active Inference and install bidirectional flow rules.
        """
        fwd_candidates = self._topo.get_candidate_paths(
            src_ip, dst_ip, self._hosts
        )
        if not fwd_candidates:
            return

        rev_candidates = self._topo.get_candidate_paths(
            dst_ip, src_ip, self._hosts
        )
        if not rev_candidates:
            rev_candidates = [list(reversed(fwd_candidates[0]))]

        fwd_key = (src_ip, dst_ip)
        rev_key = (dst_ip, src_ip)

        # ── Forward direction ─────────────────────────────────────────────────
        fwd_path = self._pick_path(fwd_key, fwd_candidates)
        # ── Reverse direction ─────────────────────────────────────────────────
        rev_path = self._pick_path(rev_key, rev_candidates)

        # Install bidirectional rules
        fm.install_bidirectional_flows(
            src_ip, dst_ip,
            fwd_path, rev_path,
            self._hosts, self._topo,
            logger=self.logger,
        )

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
                }
                self._flows[flow_key] = flow
                return candidates[0]

            beliefs = flow["beliefs"]
            active_idx = flow["path_idx"]
            # Resize if topology changed
            if len(beliefs) != len(candidates):
                new_beliefs = {i: beliefs.get(i, PathBelief())
                               for i in range(len(candidates))}
                flow["beliefs"] = new_beliefs
                beliefs = new_beliefs

        best_idx = select_best_path(
            flow_key, candidates, beliefs, active_idx,
            flow.get("load_estimate", 0.0),
            logger=self.logger,
        )

        with self._lock:
            if best_idx != flow["path_idx"]:
                self.logger.info(
                    "Flow %s: rerouting path%d -> path%d",
                    flow_key, flow["path_idx"], best_idx,
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
                # Last switch — deliver to host
                if dst_loc is not None and dst_loc[0] == dpid:
                    return dst_loc[1]
        return None

    # =========================================================================
    #  Monitor loop — port stats + inference cycle
    # =========================================================================

    def _monitor_loop(self):
        hub.sleep(5)   # wait for topology to stabilise
        while True:
            for dp in self._topo.all_datapaths():
                self._request_port_stats(dp)
            hub.sleep(POLL_INTERVAL)

    def _request_port_stats(self, dp):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY))

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
        Update PathBeliefs for all flows, re-run EFE, reroute if beneficial,
        then export state.json.
        """
        events = []

        with self._lock:
            flow_keys = list(self._flows.keys())

        for flow_key in flow_keys:
            src_ip, dst_ip = flow_key
            candidates = self._topo.get_candidate_paths(
                src_ip, dst_ip, self._hosts
            )
            if not candidates:
                continue

            with self._lock:
                flow = self._flows.get(flow_key)
            if flow is None:
                continue

            beliefs = flow["beliefs"]
            active_idx = flow["path_idx"]

            # Update beliefs with measured utilisation
            for i, path in enumerate(candidates):
                util = self._topo.path_max_util(path)
                beliefs[i].update(util)

            for i in range(len(candidates)):
                if i == active_idx:
                    beliefs[i].reinforce_confidence()
                else:
                    beliefs[i].decay_confidence()

            # Estimate flow's contribution to active path
            active_util = beliefs[active_idx].mu
            idle_utils = [
                beliefs[i].mu for i in range(len(candidates))
                if i != active_idx
            ]
            min_idle = min(idle_utils) if idle_utils else 0.0
            load_est = max(0.0, active_util - min_idle)
            load_est = 0.6 * flow.get("load_estimate", 0.0) + 0.4 * load_est
            with self._lock:
                flow["load_estimate"] = load_est

            best_idx = select_best_path(
                flow_key, candidates, beliefs, active_idx, load_est,
                logger=self.logger,
            )

            if best_idx != active_idx:
                with self._lock:
                    flow["path_idx"] = best_idx
                    flow["path"] = candidates[best_idx]

                rev_candidates = self._topo.get_candidate_paths(
                    dst_ip, src_ip, self._hosts
                ) or [list(reversed(candidates[best_idx]))]
                rev_path = self._pick_path((dst_ip, src_ip), rev_candidates)

                fm.install_bidirectional_flows(
                    src_ip, dst_ip,
                    candidates[best_idx], rev_path,
                    self._hosts, self._topo,
                    logger=self.logger,
                )
                events.append(
                    f"REROUTED {src_ip}->{dst_ip}: "
                    f"path{active_idx}->path{best_idx}"
                )
            else:
                events.append(
                    f"Held {src_ip}->{dst_ip} on path{active_idx} "
                    f"(util={active_util:.2f})"
                )

        event_str = " | ".join(events) if events else "Monitoring..."

        with self._lock:
            flows_snapshot = dict(self._flows)

        write_state(
            self._topo, self._hosts, flows_snapshot,
            event=event_str, path=STATE_JSON_PATH,
        )
