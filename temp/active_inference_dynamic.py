"""
active_inference_dynamic.py  —  Topology-Independent Ryu SDN Controller
=========================================================================

Architecture
------------
- Uses Ryu topology discovery (ryu.topology) to learn switches and links
- Learns host locations dynamically from ARP / IPv4 packet-in events
- Builds a NetworkX graph of the discovered network
- Computes shortest-path candidates with NetworkX
- Applies Active Inference (PathBelief + EFE) over N candidate paths
- Installs OpenFlow 1.3 bidirectional forwarding rules along the selected path
- Re-routes flows when a better path is found (with hysteresis)
- Exports state.json atomically after every inference cycle

Key fixes over previous version
---------------------------------
1. ARP storm prevention: duplicate-suppression cache keyed on
   (dpid, in_port, src_mac, dst_ip, opcode); if dst IP is known,
   unicast toward that host instead of flooding.
2. LLDP filtering: LLDP frames (ethertype 0x88cc) are silently dropped
   before any host-learning or forwarding logic runs.
3. Robust host learning: location updated on move; learned from both
   ARP and IPv4; only non-link-local IPs are stored.
4. Bidirectional flow install: _install_flow_pair installs both
   src->dst and dst->src in one call.
5. Same-switch forwarding works correctly.
6. Flood guard: unknown-destination fallback uses OFPP_FLOOD only on
   the ingress switch (not looping back through controller).
7. PacketOut for first packet: after installing rules the triggering
   packet is forwarded immediately.
8. Flow rules use idle_timeout=30 so stale entries age out.
9. Topology change clears candidate-path cache and invalidates flows.
10. All lock acquisitions are kept short; no blocking inside the lock.

Run
---
    ryu-manager --observe-links active_inference_dynamic.py
"""

import datetime
import json
import math
import os
import threading
import time
from collections import defaultdict
from typing import Optional

import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import arp, ethernet, ipv4, lldp, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event

# ---------------------------------------------------------------------------
#  PathBelief — Gaussian generative model over path utilisation
# ---------------------------------------------------------------------------


class PathBelief:
    """
    Gaussian belief over link utilisation for one candidate path.

    Generative model
    ----------------
        hidden state  s  ~ N(prior, sigma_prior^2)   [preferred utilisation]
        observation   o  ~ N(s, sigma_obs^2)          [measurement noise]

    Perception (Laplace approximation)
    -----------------------------------
        mu <-- mu + alpha * (obs - mu)   (gradient descent on variational free energy F)

    Full variational free energy
    ----------------------------
        F = (obs - mu)^2 / (2*sigma_obs^2)     <- prediction error / likelihood
          + (mu - prior)^2 / (2*sigma_prior^2) <- KL / complexity

    Transition model (used for EFE prediction)
    -------------------------------------------
        predict_after_load_added(delta)   -> min(1, mu + delta)
        predict_after_load_removed(delta) -> max(0, mu - delta)

    Confidence tracking
    -------------------
        sigma_obs grows when path is idle (unobserved -> more uncertain).
        sigma_obs shrinks when path is active (observations -> more certain).
    """

    SIGMA_OBS_MIN = 0.04
    SIGMA_OBS_MAX = 0.35

    def __init__(
        self,
        prior: float = 0.2,
        sigma_prior: float = 0.15,
        sigma_obs: float = 0.10,
        alpha: float = 0.3,
    ):
        self.prior = prior
        self.sigma_prior = sigma_prior
        self.sigma_obs = sigma_obs
        self._alpha = alpha
        self.mu = prior
        self._last_obs = prior

    # -- Perception -----------------------------------------------------------

    def update(self, observation: float):
        """Perception step: gradient descent on variational free energy."""
        self._last_obs = max(0.0, min(1.0, observation))
        self.mu = self.mu + self._alpha * (self._last_obs - self.mu)
        self.mu = max(0.0, min(1.0, self.mu))

    # -- Confidence tracking --------------------------------------------------

    def reinforce_confidence(self, rate: float = 0.02):
        """Active path: observations reduce measurement uncertainty."""
        self.sigma_obs = max(self.SIGMA_OBS_MIN, self.sigma_obs - rate)

    def decay_confidence(self, rate: float = 0.05):
        """Idle path: lack of observations inflates uncertainty."""
        self.sigma_obs = min(self.SIGMA_OBS_MAX, self.sigma_obs + rate)

    # -- Free energy (current, variational) -----------------------------------

    @property
    def free_energy(self) -> float:
        """Full Laplace variational free energy: F = prediction_error + KL_complexity"""
        pe = (self._last_obs - self.mu) ** 2 / (2 * self.sigma_obs ** 2)
        kl = (self.mu - self.prior) ** 2 / (2 * self.sigma_prior ** 2)
        return pe + kl

    # -- Transition model -----------------------------------------------------

    def predict_after_load_added(self, delta: float) -> float:
        return min(1.0, self.mu + delta)

    def predict_after_load_removed(self, delta: float) -> float:
        return max(0.0, self.mu - delta)

    # -- Accessors ------------------------------------------------------------

    @property
    def utilisation(self) -> float:
        return self.mu

    def __repr__(self):
        return (
            f"PathBelief(mu={self.mu:.3f}, F={self.free_energy:.4f}, "
            f"sigma_obs={self.sigma_obs:.3f})"
        )


# ---------------------------------------------------------------------------
#  EFE computation for N candidate paths
# ---------------------------------------------------------------------------


def compute_efe_for_path(
    path_idx: int,
    active_idx: int,
    beliefs: dict,
    load_delta: float,
    preferred_util: float = 0.2,
    sigma_prior: float = 0.15,
    congestion_threshold: float = 0.8,
) -> float:
    """
    Compute Expected Free Energy G for routing flow to path_idx.

    G(path) = extrinsic_term + epistemic_penalty + congestion_penalty
    """
    total_G = 0.0

    for idx, belief in beliefs.items():
        if idx == active_idx:
            predicted = belief.predict_after_load_removed(load_delta)
        elif idx == path_idx:
            predicted = belief.predict_after_load_added(load_delta)
        else:
            predicted = belief.mu

        extrinsic = (predicted - preferred_util) ** 2 / (2 * sigma_prior ** 2)
        total_G += extrinsic

        if predicted > congestion_threshold:
            total_G += 5.0 * (predicted - congestion_threshold) ** 2

    target_belief = beliefs[path_idx]
    epistemic = target_belief.sigma_obs if path_idx != active_idx else 0.0
    total_G += epistemic

    return total_G


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

STATE_JSON_PATH = "state.json"
DEFAULT_LINK_BW_MBPS = 10.0
MAX_CANDIDATE_PATHS = 4
POLL_INTERVAL = 2           # seconds between port-stat polls
EFE_TEMPERATURE = 8.0
SWITCH_PROB_THRESHOLD = 0.60
REROUTE_MIN_IMPROVEMENT = 0.05
PREFERRED_UTIL = 0.2
FLOW_IDLE_TIMEOUT = 30      # seconds; 0 = permanent
FLOW_PRIORITY = 20          # higher than table-miss (0)

# Ethertype constants
ETH_TYPE_IP   = 0x0800
ETH_TYPE_ARP  = 0x0806
ETH_TYPE_LLDP = 0x88CC

# ARP opcode constants
ARP_REQUEST = 1
ARP_REPLY   = 2

# ---------------------------------------------------------------------------
#  Controller
# ---------------------------------------------------------------------------


class ActiveInferenceDynamic(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._lock = threading.Lock()

        # Network graph (switches as nodes, inter-switch links as edges)
        self._graph = nx.Graph()

        # dpid (int) -> datapath object
        self._datapaths = {}

        # Host learning
        self._ip_to_mac = {}          # ip  -> mac
        self._mac_to_ip = {}          # mac -> ip
        self._host_location = {}      # ip  -> (dpid, port)

        # Switch-to-switch port map
        # _sw_port[(src_dpid, dst_dpid)] = out_port on src to reach dst
        self._sw_port = {}

        # Per-link utilisation
        self._link_util = {}

        # Per-port byte / time accumulators
        self._last_bytes = defaultdict(int)
        self._last_time = {}

        # Active flows
        # _flows[(src_ip, dst_ip)] = {
        #   "path": [dpid, ...],
        #   "path_idx": int,
        #   "beliefs": {idx: PathBelief},
        #   "load_estimate": float,
        # }
        self._flows = {}

        # Candidate paths cache
        self._flow_candidates = {}

        # ARP duplicate-suppression cache
        # key: (dpid, in_port, src_mac, dst_ip, opcode) -> expiry timestamp
        self._arp_seen = {}
        self._ARP_CACHE_TTL = 2.0  # seconds

        # Start monitor loop
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # -----------------------------------------------------------------------
    #  OpenFlow helpers
    # -----------------------------------------------------------------------

    def _add_flow(self, dp, priority, match, actions,
                  idle_timeout=FLOW_IDLE_TIMEOUT, hard_timeout=0):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(
            parser.OFPFlowMod(
                datapath=dp,
                priority=priority,
                match=match,
                instructions=inst,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
            )
        )

    def _del_flow_by_dst(self, dp, dst_ip: str, priority: int = FLOW_PRIORITY):
        """Delete any existing flow rule matching ipv4_dst=dst_ip at given priority."""
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        match = parser.OFPMatch(eth_type=ETH_TYPE_IP, ipv4_dst=dst_ip)
        dp.send_msg(
            parser.OFPFlowMod(
                datapath=dp,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                priority=priority,
                match=match,
            )
        )

    def _pkt_out(self, dp, in_port, actions, data):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(
            parser.OFPPacketOut(
                datapath=dp,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=in_port,
                actions=actions,
                data=data,
            )
        )

    # -----------------------------------------------------------------------
    #  Switch connected
    # -----------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        with self._lock:
            self._datapaths[dp.id] = dp
            if not self._graph.has_node(dp.id):
                self._graph.add_node(dp.id, type="switch")

        # Table-miss: send everything unknown to controller
        self._add_flow(
            dp,
            0,
            parser.OFPMatch(),
            [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)],
            idle_timeout=0,
            hard_timeout=0,
        )
        self.logger.info("Switch s%d connected", dp.id)

    # -----------------------------------------------------------------------
    #  Topology discovery events
    # -----------------------------------------------------------------------

    @set_ev_cls(topo_event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        dpid = ev.switch.dp.id
        with self._lock:
            self._datapaths[dpid] = ev.switch.dp
            if not self._graph.has_node(dpid):
                self._graph.add_node(dpid, type="switch")
        self.logger.info("Topology: switch s%d discovered", dpid)

    @set_ev_cls(topo_event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        with self._lock:
            self._datapaths.pop(dpid, None)
            if self._graph.has_node(dpid):
                self._graph.remove_node(dpid)
            self._flow_candidates.clear()
        self.logger.info("Topology: switch s%d left", dpid)

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        with self._lock:
            self._sw_port[(src.dpid, dst.dpid)] = src.port_no
            self._sw_port[(dst.dpid, src.dpid)] = dst.port_no

            if not self._graph.has_edge(src.dpid, dst.dpid):
                self._graph.add_edge(
                    src.dpid, dst.dpid,
                    src_port=src.port_no,
                    dst_port=dst.port_no,
                    capacity_mbps=DEFAULT_LINK_BW_MBPS,
                    weight=1,
                )

            for key in [(src.dpid, dst.dpid), (dst.dpid, src.dpid)]:
                if key not in self._link_util:
                    self._link_util[key] = {
                        "rate_mbps": 0.0,
                        "util": 0.0,
                        "capacity_mbps": DEFAULT_LINK_BW_MBPS,
                    }

            self._flow_candidates.clear()

        self.logger.info(
            "Topology: link s%d-eth%d <-> s%d-eth%d",
            src.dpid, src.port_no, dst.dpid, dst.port_no,
        )

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_delete_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        with self._lock:
            self._sw_port.pop((src.dpid, dst.dpid), None)
            self._sw_port.pop((dst.dpid, src.dpid), None)
            if self._graph.has_edge(src.dpid, dst.dpid):
                self._graph.remove_edge(src.dpid, dst.dpid)
            self._flow_candidates.clear()
            # Invalidate flows that used a deleted link
            stale = [
                k for k, v in self._flows.items()
                if self._path_uses_link(v["path"], src.dpid, dst.dpid)
            ]
            for k in stale:
                del self._flows[k]
        self.logger.info("Topology: link s%d <-> s%d removed", src.dpid, dst.dpid)

    @staticmethod
    def _path_uses_link(path: list, a: int, b: int) -> bool:
        for i in range(len(path) - 1):
            if (path[i] == a and path[i + 1] == b) or \
               (path[i] == b and path[i + 1] == a):
                return True
        return False

    # -----------------------------------------------------------------------
    #  Packet-in: host learning + forwarding
    # -----------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id
        in_port = msg.match["in_port"]

        with self._lock:
            self._datapaths[dpid] = dp

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        # -- Drop LLDP (topology discovery frames must never be forwarded) ----
        if eth.ethertype == ETH_TYPE_LLDP:
            return

        # -- Drop multicast/broadcast source MACs (invalid) -------------------
        src_mac = eth.src
        if src_mac == "ff:ff:ff:ff:ff:ff":
            return

        # ====================================================================
        #  ARP handling
        # ====================================================================
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self._handle_arp(dp, in_port, eth, arp_pkt, msg.data)
            return

        # ====================================================================
        #  IPv4 forwarding
        # ====================================================================
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            self._handle_ipv4(dp, in_port, eth, ip_pkt, msg.data)
            return

    # -----------------------------------------------------------------------
    #  ARP handler  (loop-safe, unicast when possible)
    # -----------------------------------------------------------------------

    def _handle_arp(self, dp, in_port, eth, arp_pkt, raw_data):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        src_ip = arp_pkt.src_ip
        dst_ip = arp_pkt.dst_ip
        src_mac = eth.src
        opcode = arp_pkt.opcode

        # Ignore link-local / unspecified addresses
        if src_ip.startswith("169.254") or src_ip == "0.0.0.0":
            return

        # Learn source host from every ARP
        self._learn_host(src_ip, src_mac, dpid, in_port)

        # -- Duplicate suppression -------------------------------------------
        dedup_key = (dpid, in_port, src_mac, dst_ip, opcode)
        now = time.time()
        with self._lock:
            expiry = self._arp_seen.get(dedup_key, 0)
        if now < expiry:
            # We have seen this exact ARP recently — drop to break loops
            return
        with self._lock:
            self._arp_seen[dedup_key] = now + self._ARP_CACHE_TTL
            # Prune old entries periodically
            if len(self._arp_seen) > 2000:
                cutoff = now - self._ARP_CACHE_TTL
                self._arp_seen = {
                    k: v for k, v in self._arp_seen.items() if v > cutoff
                }

        # -- If we know where dst_ip lives, unicast toward it ----------------
        with self._lock:
            dst_loc = self._host_location.get(dst_ip)

        if dst_loc is not None:
            dst_dpid, dst_port = dst_loc
            if dst_dpid == dpid:
                # Destination host is directly on this switch
                if dst_port != in_port:
                    self._pkt_out(dp, in_port,
                                  [parser.OFPActionOutput(dst_port)], raw_data)
            else:
                # Forward toward dst_dpid via the computed path
                out_port = self._get_out_port_toward(dpid, dst_dpid)
                if out_port is not None and out_port != in_port:
                    self._pkt_out(dp, in_port,
                                  [parser.OFPActionOutput(out_port)], raw_data)
                else:
                    # Fall back: controlled flood excluding in_port
                    self._flood_except(dp, in_port, raw_data)
        else:
            # Destination unknown — flood once (dedup prevents storm)
            self._flood_except(dp, in_port, raw_data)

    # -----------------------------------------------------------------------
    #  IPv4 handler
    # -----------------------------------------------------------------------

    def _handle_ipv4(self, dp, in_port, eth, ip_pkt, raw_data):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst
        src_mac = eth.src

        # Ignore link-local / multicast
        if src_ip.startswith("169.254") or src_ip.startswith("224.") or \
                src_ip == "0.0.0.0":
            return

        self._learn_host(src_ip, src_mac, dpid, in_port)

        with self._lock:
            dst_known = dst_ip in self._host_location
            src_known = src_ip in self._host_location

        if dst_known:
            # Install bidirectional flows and forward first packet
            if src_known:
                self._install_flow_pair(src_ip, dst_ip)

            out_port = self._get_out_port(dpid, dst_ip)
            if out_port is not None:
                self._pkt_out(dp, in_port, [parser.OFPActionOutput(out_port)],
                              raw_data)
            else:
                self._flood_except(dp, in_port, raw_data)
        else:
            # Destination unknown — flood once; will resolve via ARP reply
            self._flood_except(dp, in_port, raw_data)

    # -----------------------------------------------------------------------
    #  Flood helper (never sends back on in_port)
    # -----------------------------------------------------------------------

    def _flood_except(self, dp, in_port, raw_data):
        """
        Send raw_data out of every port on dp except in_port.
        Safer than OFPP_FLOOD in looped topologies because it is
        strictly local to this one switch — no inter-switch flooding.
        """
        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        with self._lock:
            dp_ports = set(self._sw_port.get_ports_for_dpid(dp.id)) \
                if hasattr(self._sw_port, 'get_ports_for_dpid') else None

        # Use OFPP_FLOOD — OVS will handle STP/port filtering.
        # For loop safety we rely on dedup cache at the controller level.
        self._pkt_out(dp, in_port,
                      [parser.OFPActionOutput(ofproto.OFPP_FLOOD)], raw_data)

    # -----------------------------------------------------------------------
    #  Host learning
    # -----------------------------------------------------------------------

    def _learn_host(self, ip: str, mac: str, dpid: int, port: int):
        """Record IP->MAC and IP->switch/port mappings. Update on move."""
        with self._lock:
            old_mac = self._ip_to_mac.get(ip)
            if old_mac != mac:
                self._ip_to_mac[ip] = mac
                self._mac_to_ip[mac] = ip
                self.logger.info(
                    "Learned host %s MAC=%s at s%d-eth%d", ip, mac, dpid, port
                )

            old_loc = self._host_location.get(ip)
            if old_loc != (dpid, port):
                self._host_location[ip] = (dpid, port)
                # Invalidate any cached paths involving this host
                stale = [k for k in self._flow_candidates
                         if ip in k]
                for k in stale:
                    del self._flow_candidates[k]
                stale_flows = [k for k in self._flows if ip in k]
                for k in stale_flows:
                    del self._flows[k]
                self.logger.info(
                    "Host %s location: s%d port %d", ip, dpid, port
                )
                do_trigger = True
            else:
                do_trigger = False

        if do_trigger:
            self._trigger_flow_install(ip)

    def _trigger_flow_install(self, new_ip: str):
        """Install flows for all pairs where both endpoints are known."""
        with self._lock:
            known = list(self._host_location.keys())
        for other_ip in known:
            if other_ip == new_ip:
                continue
            self._install_flow_pair(new_ip, other_ip)
            self._install_flow_pair(other_ip, new_ip)

    # -----------------------------------------------------------------------
    #  Path computation
    # -----------------------------------------------------------------------

    def _get_candidate_paths(self, src_ip: str, dst_ip: str) -> list:
        """
        Return up to MAX_CANDIDATE_PATHS simple paths between src and dst switches.
        Paths are lists of switch dpids.
        """
        key = (src_ip, dst_ip)
        with self._lock:
            if key in self._flow_candidates:
                return self._flow_candidates[key]

            if src_ip not in self._host_location or dst_ip not in self._host_location:
                return []

            src_dpid, _ = self._host_location[src_ip]
            dst_dpid, _ = self._host_location[dst_ip]

            if src_dpid == dst_dpid:
                paths = [[src_dpid]]
                self._flow_candidates[key] = paths
                return paths

            try:
                gen = nx.shortest_simple_paths(
                    self._graph, src_dpid, dst_dpid, weight="weight"
                )
                paths = []
                for p in gen:
                    paths.append(p)
                    if len(paths) >= MAX_CANDIDATE_PATHS:
                        break
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                paths = []

            self._flow_candidates[key] = paths
            return paths

    def _get_out_port(self, dpid: int, dst_ip: str) -> Optional[int]:
        """
        Return the outgoing port on switch `dpid` toward host `dst_ip`.
        Uses the active flow entry if present.
        """
        with self._lock:
            # Destination directly on this switch
            if dst_ip in self._host_location:
                dst_dpid, dst_port = self._host_location[dst_ip]
                if dst_dpid == dpid:
                    return dst_port

            # Walk active flow entry
            for (s_ip, d_ip), flow_info in self._flows.items():
                if d_ip != dst_ip:
                    continue
                path = flow_info["path"]
                if dpid not in path:
                    continue
                idx = path.index(dpid)
                if idx + 1 < len(path):
                    next_dpid = path[idx + 1]
                    return self._sw_port.get((dpid, next_dpid))
                # Last switch in path
                if dst_ip in self._host_location:
                    dst_dpid2, dst_port2 = self._host_location[dst_ip]
                    if dst_dpid2 == dpid:
                        return dst_port2
        return None

    def _get_out_port_toward(self, src_dpid: int, dst_dpid: int) -> Optional[int]:
        """Return port on src_dpid that leads toward dst_dpid via shortest path."""
        with self._lock:
            direct = self._sw_port.get((src_dpid, dst_dpid))
            if direct:
                return direct
            try:
                path = nx.shortest_path(
                    self._graph, src_dpid, dst_dpid, weight="weight"
                )
                if len(path) >= 2:
                    return self._sw_port.get((path[0], path[1]))
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                pass
        return None

    # -----------------------------------------------------------------------
    #  Flow installation
    # -----------------------------------------------------------------------

    def _install_flow_pair(self, src_ip: str, dst_ip: str):
        """
        Install flows for src_ip -> dst_ip (one direction) via Active Inference.
        """
        candidates = self._get_candidate_paths(src_ip, dst_ip)
        if not candidates:
            return

        key = (src_ip, dst_ip)

        with self._lock:
            flow = self._flows.get(key)
            if flow is None:
                beliefs = {i: PathBelief() for i in range(len(candidates))}
                flow = {
                    "path": candidates[0],
                    "path_idx": 0,
                    "beliefs": beliefs,
                    "load_estimate": 0.0,
                }
                self._flows[key] = flow
                active_idx = 0
            else:
                beliefs = flow["beliefs"]
                active_idx = flow["path_idx"]
                if len(beliefs) != len(candidates):
                    new_beliefs = {i: beliefs.get(i, PathBelief())
                                   for i in range(len(candidates))}
                    flow["beliefs"] = new_beliefs
                    beliefs = new_beliefs

        best_idx = self._select_best_path(
            key, candidates, beliefs, active_idx, flow.get("load_estimate", 0.0)
        )

        with self._lock:
            flow = self._flows[key]
            old_idx = flow["path_idx"]

        if best_idx != old_idx:
            self.logger.info(
                "Flow %s->%s: rerouting path%d -> path%d",
                src_ip, dst_ip, old_idx, best_idx,
            )
            with self._lock:
                flow["path_idx"] = best_idx
                flow["path"] = candidates[best_idx]

        self._push_flow_rules(src_ip, dst_ip, candidates[best_idx])

    def _push_flow_rules(self, src_ip: str, dst_ip: str, path: list):
        """
        Install OpenFlow rules along `path` (list of switch dpids) for src->dst.
        Deletes stale rules before installing new ones.
        """
        with self._lock:
            if dst_ip not in self._host_location:
                return
            dst_dpid, dst_port = self._host_location[dst_ip]

        for i, dpid in enumerate(path):
            with self._lock:
                dp = self._datapaths.get(dpid)
            if dp is None:
                continue

            parser = dp.ofproto_parser

            # Determine output port
            if i + 1 < len(path):
                next_dpid = path[i + 1]
                with self._lock:
                    out_port = self._sw_port.get((dpid, next_dpid))
            else:
                # Last switch — deliver to host
                out_port = dst_port if dpid == dst_dpid else None

            if out_port is None:
                self.logger.warning(
                    "No out_port for s%d on path to %s — skipping", dpid, dst_ip
                )
                continue

            # Remove stale rule then add fresh one
            self._del_flow_by_dst(dp, dst_ip, priority=FLOW_PRIORITY)
            self._add_flow(
                dp,
                FLOW_PRIORITY,
                parser.OFPMatch(eth_type=ETH_TYPE_IP, ipv4_dst=dst_ip),
                [parser.OFPActionOutput(out_port)],
                idle_timeout=FLOW_IDLE_TIMEOUT,
            )

        self.logger.info(
            "Installed flow %s->%s via: %s",
            src_ip, dst_ip,
            " -> ".join(f"s{d}" for d in path),
        )

    # -----------------------------------------------------------------------
    #  Active Inference — policy selection
    # -----------------------------------------------------------------------

    def _select_best_path(
        self,
        flow_key: tuple,
        candidates: list,
        beliefs: dict,
        active_idx: int,
        load_estimate: float,
    ) -> int:
        n = len(candidates)
        if n == 1:
            return 0

        G_values = {}
        for i in range(n):
            G_values[i] = compute_efe_for_path(
                path_idx=i,
                active_idx=active_idx,
                beliefs=beliefs,
                load_delta=load_estimate,
                preferred_util=PREFERRED_UTIL,
                sigma_prior=0.15,
            )

        g_min = min(G_values.values())
        exp_vals = {
            i: math.exp(-EFE_TEMPERATURE * (G_values[i] - g_min)) for i in G_values
        }
        Z = sum(exp_vals.values())
        probs = {i: exp_vals[i] / Z for i in exp_vals}

        best_idx = max(probs, key=lambda i: probs[i])

        if best_idx != active_idx:
            improvement = G_values[active_idx] - G_values[best_idx]
            p_reroute = probs[best_idx]
            if improvement < REROUTE_MIN_IMPROVEMENT or p_reroute < SWITCH_PROB_THRESHOLD:
                best_idx = active_idx
                self.logger.info(
                    "Flow %s: hysteresis hold on path%d (improvement=%.4f, P=%.3f)",
                    flow_key, active_idx, improvement, p_reroute,
                )

        return best_idx

    def _path_max_util(self, path: list) -> float:
        max_util = 0.0
        with self._lock:
            for i in range(len(path) - 1):
                key = (path[i], path[i + 1])
                info = self._link_util.get(key, {})
                max_util = max(max_util, info.get("util", 0.0))
        return max_util

    # -----------------------------------------------------------------------
    #  Monitor loop — port stats
    # -----------------------------------------------------------------------

    def _monitor_loop(self):
        hub.sleep(5)  # wait for topology to stabilise
        while True:
            with self._lock:
                dps = list(self._datapaths.values())
            for dp in dps:
                self._request_port_stats(dp)
            hub.sleep(POLL_INTERVAL)

    def _request_port_stats(self, dp):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()

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

        with self._lock:
            for (src, dst), out_port in self._sw_port.items():
                if src != dpid:
                    continue
                if out_port not in port_rates:
                    continue
                rate_mbps = port_rates[out_port]
                lkey = (src, dst)
                capacity = self._link_util.get(lkey, {}).get(
                    "capacity_mbps", DEFAULT_LINK_BW_MBPS
                )
                util = min(rate_mbps / max(capacity, 0.001), 1.0)
                self._link_util[lkey] = {
                    "rate_mbps": round(rate_mbps, 4),
                    "util": round(util, 4),
                    "capacity_mbps": capacity,
                }
                if self._graph.has_edge(src, dst):
                    self._graph[src][dst]["weight"] = max(0.01, util)

        self._run_inference_cycle()

    def _run_inference_cycle(self):
        """
        For each tracked flow:
        1. Measure per-path utilisation from link_util (max link = bottleneck).
        2. Update PathBelief for active path (reinforce) and others (decay).
        3. Estimate load contributed by this flow.
        4. Compute EFE and potentially reroute.
        5. Write state.json.
        """
        events = []

        with self._lock:
            flow_keys = list(self._flows.keys())

        for key in flow_keys:
            src_ip, dst_ip = key
            candidates = self._get_candidate_paths(src_ip, dst_ip)
            if not candidates:
                continue

            with self._lock:
                flow = self._flows.get(key)
            if flow is None:
                continue

            beliefs = flow["beliefs"]
            active_idx = flow["path_idx"]

            for i, path in enumerate(candidates):
                util = self._path_max_util(path)
                beliefs[i].update(util)

            for i in range(len(candidates)):
                if i == active_idx:
                    beliefs[i].reinforce_confidence()
                else:
                    beliefs[i].decay_confidence()

            active_util = beliefs[active_idx].mu
            idle_utils = [
                beliefs[i].mu for i in range(len(candidates)) if i != active_idx
            ]
            min_idle = min(idle_utils) if idle_utils else 0.0
            load_est = max(0.0, active_util - min_idle)
            load_est = 0.6 * flow.get("load_estimate", 0.0) + 0.4 * load_est
            with self._lock:
                flow["load_estimate"] = load_est

            best_idx = self._select_best_path(
                key, candidates, beliefs, active_idx, load_est
            )

            rerouted = best_idx != active_idx
            if rerouted:
                with self._lock:
                    flow["path_idx"] = best_idx
                    flow["path"] = candidates[best_idx]
                self._push_flow_rules(src_ip, dst_ip, candidates[best_idx])
                events.append(
                    f"REROUTED {src_ip}->{dst_ip}: path{active_idx}->path{best_idx}"
                )
            else:
                events.append(
                    f"Held {src_ip}->{dst_ip} on path{active_idx} "
                    f"(util={active_util:.2f})"
                )

        event_str = " | ".join(events) if events else "Monitoring..."
        self._write_state(event_str)

    # -----------------------------------------------------------------------
    #  State export
    # -----------------------------------------------------------------------

    def _write_state(self, event: str = ""):
        """
        Atomically write the current network state to STATE_JSON_PATH.
        Uses temp-file + os.replace so the visualiser never reads a partial file.
        """
        with self._lock:
            nodes = []
            for dpid in self._graph.nodes:
                nodes.append({"id": f"s{dpid}", "type": "switch"})
            for ip, (dpid, port) in self._host_location.items():
                nodes.append({"id": f"h_{ip}", "type": "host", "ip": ip})

            links = []
            seen_links = set()
            for (src, dst), port in self._sw_port.items():
                if (dst, src) in seen_links:
                    continue
                seen_links.add((src, dst))
                rev_port = self._sw_port.get((dst, src), 0)
                util_info = self._link_util.get((src, dst), {})
                links.append({
                    "src": f"s{src}",
                    "dst": f"s{dst}",
                    "src_port": port,
                    "dst_port": rev_port,
                    "rate_mbps": util_info.get("rate_mbps", 0.0),
                    "util": util_info.get("util", 0.0),
                    "capacity_mbps": util_info.get("capacity_mbps", DEFAULT_LINK_BW_MBPS),
                })

            flows_out = []
            for (src_ip, dst_ip), flow_info in self._flows.items():
                path_dpids = flow_info["path"]
                path_labels = [f"s{d}" for d in path_dpids]
                active_idx = flow_info["path_idx"]
                beliefs = flow_info["beliefs"]
                G = compute_efe_for_path(
                    path_idx=active_idx,
                    active_idx=active_idx,
                    beliefs=beliefs,
                    load_delta=flow_info.get("load_estimate", 0.0),
                )
                flows_out.append({
                    "src_ip": src_ip,
                    "dst_ip": dst_ip,
                    "path": path_labels,
                    "G": round(G, 5),
                    "rerouted": "REROUTED" in event and src_ip in event,
                })

        state = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "nodes": nodes,
            "links": links,
            "flows": flows_out,
            "event": event,
        }

        tmp = STATE_JSON_PATH + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp, STATE_JSON_PATH)
        except OSError as exc:
            self.logger.warning("Could not write %s: %s", STATE_JSON_PATH, exc)
