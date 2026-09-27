"""
reactive_dynamic.py  —  Topology-Independent Ryu SDN Controller (Reactive baseline)
=================================================================================

A classic **reactive** congestion-control controller, meant as a direct,
apples-to-apples comparison baseline against `active_inference_dynamic.py`.
It reuses every piece of generic SDN plumbing from that file unchanged —
topology discovery, ARP/IPv4 handling, host learning, port/flow-stat
polling, state.json export — and differs *only* in how a path is chosen.

  ai/belief.py, ai/policy.py     NOT used. No PathBelief, no Expected Free
                                  Energy, no exploration bonus, no
                                  probabilistic softmax hysteresis.
  blockchain/*                   NOT used. No model trading, no audit
                                  ledger, no controller identities. This is
                                  a single, undifferentiated controller.
  sdn/constants.py                REACTIVE_CONGESTION_THRESHOLD,
                                  REACTIVE_MIN_IMPROVEMENT (this file's only
                                  two tunables)
  sdn/topology_manager.py, sdn/host_manager.py, sdn/flow_manager.py,
  sdn/state_writer.py, sdn/topology_spec.py, utils/ip_utils.py
                                  Identical to the Active Inference
                                  controller — same topology graph, same
                                  host learning, same flow install
                                  primitives, same state.json schema (so
                                  the existing visualizer/orchestrator work
                                  with this controller with zero changes).

Routing algorithm
------------------
Deliberately the simplest thing that could be called "reactive congestion
control": no prediction, no memory of past behaviour beyond the currently
active path, no proactive load spreading.

  1. On flow install (cold start), pick the first candidate path Ryu's
     topology discovery returns — no comparison, no optimisation. A
     reactive controller doesn't try to pick well ahead of time; it only
     reacts once there's an actual problem.
  2. Every monitor tick, measure the *active* path's current bottleneck
     utilisation (TopologyManager.path_max_util — the same measurement
     the Active Inference controller uses, for a fair comparison).
  3. If that utilisation is below REACTIVE_CONGESTION_THRESHOLD: do
     nothing. This is the defining trait of "reactive" vs. "active
     inference" — a lightly-loaded path is never proactively rebalanced
     just because a nominally-better alternative exists.
  4. If it's at/above threshold: look at every candidate path's current
     utilisation and pick the least-loaded one.
       - If it's at least REACTIVE_MIN_IMPROVEMENT better than the active
         path, switch to it outright (a single, full commit — no partial
         split, no continuous weighting).
       - Otherwise (every candidate is roughly equally congested — moving
         wouldn't clearly help), fall back to a plain equal-weight ECMP
         SELECT group across all candidates: naive, undifferentiated
         hash-based load spreading, the standard "we're out of better
         ideas" reactive fallback, as opposed to the Active Inference
         controller's continuously headroom-weighted split.

Run
---
    ryu-manager --observe-links reactive_dynamic.py

Verify
------
    mininet> pingall
    mininet> h1 ping h2
    mininet> iperf h1 h2
    mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
    mininet> sh ovs-ofctl -O OpenFlow13 dump-groups s1
"""

import os
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

from sdn import flow_manager as fm
from sdn import topology_spec
from sdn.constants import (
    ARP_CACHE_TTL,
    ETH_TYPE_LLDP,
    LINK_FLAP_GRACE_SEC,
    POLL_INTERVAL,
    REACTIVE_CONGESTION_THRESHOLD,
    REACTIVE_MIN_IMPROVEMENT,
    STATE_JSON_PATH,
)
from sdn.host_manager import HostManager
from sdn.state_writer import write_state
from sdn.topology_manager import TopologyManager


class ReactiveController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._lock = threading.Lock()

        # Managers — identical to the Active Inference controller.
        self._topo = TopologyManager()
        self._hosts = HostManager()

        # Active flows. Deliberately minimal compared to the Active
        # Inference controller's per-flow state: no beliefs, no owning
        # controller, no trained model — just "which path are we on".
        # _flows[(src_ip, dst_ip)] = {
        #   "path":          [dpid, ...],   the currently active path
        #   "path_idx":      int,
        #   "load_estimate": float,         EMA'd utilisation, same math
        #                                   as the Active Inference
        #                                   controller so thresholds are
        #                                   comparable apples-to-apples
        #   "multipath":     bool,          True when a plain ECMP SELECT
        #                                   group is active
        #   "ticks":         int,
        # }
        self._flows = {}

        # Per-port byte / time accumulators
        self._last_bytes = defaultdict(int)  # (dpid, port_no) -> bytes
        self._last_time = {}  # (dpid, port_no) -> timestamp

        # Per-port packet / drop counters (kept for the dashboard's link
        # display even though this controller has no CIU/audit layer).
        self._last_pkts = defaultdict(int)  # (dpid, port_no) -> rx+tx packets
        self._last_drops = defaultdict(int)  # (dpid, port_no) -> rx+tx dropped

        # Per-flow byte accumulators for direct load measurement.
        self._flow_bytes = {}

        # ARP duplicate-suppression cache
        self._arp_seen = {}

        # Pending link removals, for flap debouncing.
        self._pending_link_removal = {}

        # ── Topology spec (bandwidth source of truth) ───────────────────────
        # Same as the Active Inference controller: real link capacities
        # feed correct utilisation math, and the file is re-checked every
        # monitor tick so an "Apply topology" from the GUI is picked up
        # live without restarting this controller.
        self._spec_path = os.environ.get(
            "SDN_TOPOLOGY_SPEC", topology_spec.DEFAULT_SPEC_PATH
        )
        self._spec_mtime = None
        self._reload_topology_spec(initial=True)

        # Monitor loop
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # =========================================================================
    #  Topology spec (bandwidth source of truth)
    # =========================================================================

    def _reload_topology_spec(self, initial: bool = False) -> None:
        mtime = topology_spec.spec_mtime(self._spec_path)
        if mtime is None:
            if initial:
                self.logger.info(
                    "No topology spec at %s yet — using DEFAULT_LINK_BW_MBPS "
                    "until one is applied.",
                    self._spec_path,
                )
            return
        if mtime == self._spec_mtime:
            return  # unchanged since last check

        try:
            spec = topology_spec.load_spec(self._spec_path)
        except (OSError, ValueError) as exc:
            self.logger.warning(
                "Failed to (re)load topology spec %s: %s", self._spec_path, exc
            )
            return

        self._spec_mtime = mtime
        capacities = topology_spec.link_capacity_by_dpid(spec)
        self._topo.set_link_capacities(capacities)
        self.logger.info(
            "Topology spec %s (re)loaded: %d switch<->switch link "
            "capacities applied.",
            self._spec_path,
            len(capacities) // 2,
        )

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
    #  Topology discovery events — identical to active_inference_dynamic.py
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

        greenthread = hub.spawn(
            self._apply_link_removal, src.dpid, src.port_no, dst.dpid, dst.port_no, key
        )
        self._pending_link_removal[key] = greenthread

    def _apply_link_removal(
        self, src_dpid: int, src_port: int, dst_dpid: int, dst_port: int, key: tuple
    ) -> None:
        hub.sleep(LINK_FLAP_GRACE_SEC)

        with self._lock:
            still_pending = self._pending_link_removal.get(key) is not None
        if not still_pending:
            return

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
    #  Packet-in — identical to active_inference_dynamic.py
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]

        self._topo.add_switch(dpid, dp)

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        if eth.ethertype == ETH_TYPE_LLDP:
            return

        if eth.src in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            return

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self._handle_arp(dp, in_port, eth, arp_pkt, msg.data)
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            self._handle_ipv4(dp, in_port, eth, ip_pkt, msg.data)
            return

    def _handle_arp(self, dp, in_port, eth, arp_pkt, raw_data):
        parser = dp.ofproto_parser
        dpid = dp.id

        src_ip = arp_pkt.src_ip
        dst_ip = arp_pkt.dst_ip
        src_mac = eth.src
        opcode = arp_pkt.opcode

        learned = self._hosts.learn_host(
            src_ip, src_mac, dpid, in_port, self._topo, self.logger
        )
        if learned:
            self._topo.invalidate_path_cache(src_ip)
            self._trigger_flows_for(src_ip)

        now = time.time()
        dedup_key = (dpid, in_port, src_mac, dst_ip, opcode)
        with self._lock:
            expiry = self._arp_seen.get(dedup_key, 0)
        if now < expiry:
            return
        with self._lock:
            self._arp_seen[dedup_key] = now + ARP_CACHE_TTL
            if len(self._arp_seen) > 2000:
                cutoff = now - ARP_CACHE_TTL
                self._arp_seen = {k: v for k, v in self._arp_seen.items() if v > cutoff}

        dst_loc = self._hosts.get_location(dst_ip)

        if dst_loc is not None:
            dst_dpid, dst_port = dst_loc
            if dst_dpid == dpid:
                if dst_port != in_port:
                    fm.packet_out(
                        dp, in_port, [parser.OFPActionOutput(dst_port)], raw_data
                    )
            else:
                out_port = self._topo.get_out_port_toward(dpid, dst_dpid)
                if out_port is not None and out_port != in_port:
                    fm.packet_out(
                        dp, in_port, [parser.OFPActionOutput(out_port)], raw_data
                    )
                else:
                    self._flood_safe(dp, in_port, raw_data)
        else:
            self._flood_safe(dp, in_port, raw_data)

    def _handle_ipv4(self, dp, in_port, eth, ip_pkt, raw_data):
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
                fm.packet_out(dp, in_port, [parser.OFPActionOutput(out_port)], raw_data)
            else:
                self._flood_safe(dp, in_port, raw_data)
        else:
            self._flood_safe(dp, in_port, raw_data)

    def _flood_safe(self, dp, in_port, raw_data):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        fm.packet_out(
            dp, in_port, [parser.OFPActionOutput(ofproto.OFPP_FLOOD)], raw_data
        )

    def _trigger_flows_for(self, new_ip: str) -> None:
        for other_ip in self._hosts.all_ips():
            if other_ip == new_ip:
                continue
            self._install_flow_pair(new_ip, other_ip)
            self._install_flow_pair(other_ip, new_ip)

    # =========================================================================
    #  Flow installation — reactive routing decision
    # =========================================================================

    def _install_flow_pair(self, src_ip: str, dst_ip: str) -> None:
        """
        Cold-start install: just take the first candidate path Ryu's
        topology discovery hands back — no comparison at this point. A
        reactive controller only starts reacting once a real problem
        (measured congestion) shows up on the *next* monitor tick.
        """
        fwd_candidates = self._topo.get_candidate_paths(src_ip, dst_ip, self._hosts)
        if not fwd_candidates:
            return

        rev_candidates = self._topo.get_candidate_paths(dst_ip, src_ip, self._hosts)
        if not rev_candidates:
            rev_candidates = [list(reversed(fwd_candidates[0]))]

        fwd_key = (src_ip, dst_ip)
        rev_key = (dst_ip, src_ip)

        with self._lock:
            for key, candidates in [
                (fwd_key, fwd_candidates),
                (rev_key, rev_candidates),
            ]:
                if key not in self._flows:
                    seed_load = self._topo.path_max_util(candidates[0])
                    self._flows[key] = {
                        "path": candidates[0],
                        "path_idx": 0,
                        "load_estimate": seed_load,
                        "multipath": False,
                        "ticks": 0,
                    }

        fm.install_bidirectional_flows(
            src_ip,
            dst_ip,
            self._flows[fwd_key]["path"],
            self._flows[rev_key]["path"],
            self._hosts,
            self._topo,
            logger=self.logger,
        )

    def _get_out_port(self, dpid: int, dst_ip: str):
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
    #  Monitor loop — port stats + reactive re-evaluation cycle
    # =========================================================================

    def _monitor_loop(self):
        from sdn.constants import (
            CONGESTION_INVALIDATE_THRESHOLD,
            FLOW_STATS_POLL_INTERVAL,
        )

        hub.sleep(5)  # wait for topology to stabilise
        _flow_stat_tick = 0

        while True:
            self._reload_topology_spec()

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
        port_losses = {}
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

            total_pkts = stat.rx_packets + stat.tx_packets
            total_drops = stat.rx_dropped + stat.tx_dropped
            last_p = self._last_pkts.get(key, total_pkts)
            last_d = self._last_drops.get(key, total_drops)
            pkt_delta = max(0, total_pkts - last_p)
            drop_delta = max(0, total_drops - last_d)
            if pkt_delta > 0:
                loss_fraction = drop_delta / pkt_delta
            else:
                loss_fraction = port_losses.get(port_no, 0.0)
            self._last_pkts[key] = total_pkts
            self._last_drops[key] = total_drops
            port_losses[port_no] = loss_fraction

        for (src, dst), out_port in sw_port_map.items():
            if src != dpid:
                continue
            if out_port not in port_rates:
                continue
            rate_mbps = port_rates[out_port]
            existing = self._topo.get_link_util(src, dst)
            capacity = existing.get("capacity_mbps", 10.0)
            self._topo.update_link_util(src, dst, rate_mbps, capacity)
            if out_port in port_losses:
                self._topo.update_link_drops(src, dst, port_losses[out_port])

        self._run_reactive_cycle()

    # =========================================================================
    #  Reactive re-evaluation cycle
    # =========================================================================

    def _run_reactive_cycle(self):
        """
        For every active flow: measure the active path's current
        bottleneck utilisation; below REACTIVE_CONGESTION_THRESHOLD, do
        nothing (no proactive rebalancing — the defining trait of
        "reactive" vs. Active Inference). At/above it, move to the
        least-loaded alternative if that's a clear (REACTIVE_MIN_IMPROVEMENT)
        win, otherwise fall back to a plain equal-weight ECMP SELECT group.
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

            with self._lock:
                active_idx = flow["path_idx"]
                if active_idx >= len(candidates):
                    active_idx = 0
                    flow["path_idx"] = 0
                    flow["path"] = candidates[0]
                flow["ticks"] = flow.get("ticks", 0) + 1

            # ── Direct per-flow load measurement — identical maths to the
            # Active Inference controller, so REACTIVE_CONGESTION_THRESHOLD
            # and MULTIPATH_CONGESTION_THRESHOLD are directly comparable.
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

            # ── Reactive decision — no action below threshold ─────────────────
            if load_est < REACTIVE_CONGESTION_THRESHOLD or len(candidates) <= 1:
                with self._lock:
                    flow["multipath"] = False
                events.append(
                    f"Held {src_ip}->{dst_ip} on path{active_idx} "
                    f"(util={load_est:.2f}, below threshold)"
                )
                continue

            path_utils = [self._topo.path_max_util(p) for p in candidates]
            best_idx = min(range(len(candidates)), key=lambda i: path_utils[i])
            improvement = path_utils[active_idx] - path_utils[best_idx]

            rev_candidates = self._topo.get_candidate_paths(
                dst_ip, src_ip, self._hosts
            ) or [list(reversed(candidates[active_idx]))]

            if best_idx != active_idx and improvement >= REACTIVE_MIN_IMPROVEMENT:
                # A clearly-better single alternative exists — full commit,
                # exactly the "move away from the problem" reactive action.
                with self._lock:
                    flow["path_idx"] = best_idx
                    flow["path"] = candidates[best_idx]
                    flow["multipath"] = False

                fm.install_bidirectional_flows(
                    src_ip,
                    dst_ip,
                    candidates[best_idx],
                    list(reversed(candidates[best_idx])),
                    self._hosts,
                    self._topo,
                    logger=self.logger,
                )
                events.append(
                    f"REROUTED {src_ip}->{dst_ip}: "
                    f"path{active_idx}->path{best_idx} "
                    f"(util {path_utils[active_idx]:.2f}->{path_utils[best_idx]:.2f})"
                )
            else:
                # Congested, but no single path is clearly better — the
                # standard reactive fallback is naive equal-weight ECMP,
                # not a proportional split: every candidate gets the same
                # bucket weight regardless of its exact utilisation.
                equal_weights = [1] * len(candidates)
                fm.install_multipath_flows(
                    src_ip,
                    dst_ip,
                    candidates,
                    equal_weights,
                    rev_candidates,
                    [1] * len(rev_candidates),
                    self._hosts,
                    self._topo,
                    logger=self.logger,
                )
                with self._lock:
                    flow["multipath"] = True
                events.append(
                    f"ECMP {src_ip}->{dst_ip}: load={load_est:.2f} "
                    f"equal-weight across {len(candidates)} paths"
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
            ledger=None,
        )


if __name__ == "__main__":
    import sys

    print(
        "reactive_dynamic.py is a Ryu application, not a standalone script — "
        "running it with plain `python` does nothing (no error, just an "
        "immediate exit, because the file only defines the class).\n\n"
        "Run it with:\n"
        "    ryu-manager --observe-links reactive_dynamic.py\n",
        file=sys.stderr,
    )
    sys.exit(1)
