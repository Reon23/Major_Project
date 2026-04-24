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
- Installs OpenFlow 1.3 forwarding rules along the selected path
- Re-routes flows when a better path is found (with hysteresis)
- Exports state.json atomically after every inference cycle

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
from ryu.lib.packet import arp, ethernet, ipv4, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event
from ryu.topology.api import get_switch, get_link

# ─────────────────────────────────────────────────────────────────────────────
#  PathBelief — Gaussian generative model over path utilisation
# ─────────────────────────────────────────────────────────────────────────────


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
        self.prior = prior  # preferred (low) utilisation
        self.sigma_prior = sigma_prior
        self.sigma_obs = sigma_obs  # measurement noise (adaptive)
        self._alpha = alpha
        self.mu = prior  # belief mean, initialised at prior
        self._last_obs = prior

    # ── Perception ────────────────────────────────────────────────────────────

    def update(self, observation: float):
        """Perception step: gradient descent on variational free energy."""
        self._last_obs = max(0.0, min(1.0, observation))
        self.mu = self.mu + self._alpha * (self._last_obs - self.mu)
        self.mu = max(0.0, min(1.0, self.mu))

    # ── Confidence tracking ───────────────────────────────────────────────────

    def reinforce_confidence(self, rate: float = 0.02):
        """Active path: observations reduce measurement uncertainty."""
        self.sigma_obs = max(self.SIGMA_OBS_MIN, self.sigma_obs - rate)

    def decay_confidence(self, rate: float = 0.05):
        """Idle path: lack of observations inflates uncertainty."""
        self.sigma_obs = min(self.SIGMA_OBS_MAX, self.sigma_obs + rate)

    # ── Free energy (current, variational) ───────────────────────────────────

    @property
    def free_energy(self) -> float:
        """Full Laplace variational free energy: F = prediction_error + KL_complexity"""
        pe = (self._last_obs - self.mu) ** 2 / (2 * self.sigma_obs**2)
        kl = (self.mu - self.prior) ** 2 / (2 * self.sigma_prior**2)
        return pe + kl

    # ── Transition model ──────────────────────────────────────────────────────

    def predict_after_load_added(self, delta: float) -> float:
        """Expected mu if this path absorbs an additional utilisation delta."""
        return min(1.0, self.mu + delta)

    def predict_after_load_removed(self, delta: float) -> float:
        """Expected mu if load delta leaves this path."""
        return max(0.0, self.mu - delta)

    # ── Accessors ──────────────────────────────────────────────────────────────

    @property
    def utilisation(self) -> float:
        return self.mu

    def __repr__(self):
        return (
            f"PathBelief(mu={self.mu:.3f}, F={self.free_energy:.4f}, "
            f"sigma_obs={self.sigma_obs:.3f})"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  EFE computation for N candidate paths
# ─────────────────────────────────────────────────────────────────────────────


def compute_efe_for_path(
    path_idx: int,
    active_idx: int,
    beliefs: dict,  # idx -> PathBelief
    load_delta: float,
    preferred_util: float = 0.2,
    sigma_prior: float = 0.15,
    congestion_threshold: float = 0.8,
) -> float:
    """
    Compute Expected Free Energy G for routing flow to path_idx.

    G(path) = extrinsic_term + epistemic_penalty + congestion_penalty

    Extrinsic term: predicted deviation from preferred utilisation (across all paths)
    Epistemic penalty: uncertainty of the target path (sigma_obs)
    Congestion penalty: extra cost if predicted utilisation > threshold
    """
    total_G = 0.0

    for idx, belief in beliefs.items():
        if idx == active_idx:
            # Traffic leaves the active path
            predicted = belief.predict_after_load_removed(load_delta)
        elif idx == path_idx:
            # Traffic is added to the candidate path
            predicted = belief.predict_after_load_added(load_delta)
        else:
            predicted = belief.mu

        # Extrinsic: distance from preferred utilisation
        extrinsic = (predicted - preferred_util) ** 2 / (2 * sigma_prior**2)
        total_G += extrinsic

        # Congestion penalty: exponential above threshold
        if predicted > congestion_threshold:
            total_G += 5.0 * (predicted - congestion_threshold) ** 2

    # Epistemic penalty: only for the target path
    target_belief = beliefs[path_idx]
    epistemic = target_belief.sigma_obs if path_idx != active_idx else 0.0
    total_G += epistemic

    return total_G


# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

STATE_JSON_PATH = "state.json"
DEFAULT_LINK_BW_MBPS = 10.0  # fallback link capacity
MAX_CANDIDATE_PATHS = 4  # how many shortest paths to consider per flow
POLL_INTERVAL = 2  # seconds between port-stat polls
EFE_TEMPERATURE = 8.0  # softmax temperature for policy selection
SWITCH_PROB_THRESHOLD = 0.60  # min P(reroute) before acting
REROUTE_MIN_IMPROVEMENT = 0.05  # hysteresis: G must improve by at least this much
PREFERRED_UTIL = 0.2  # preferred utilisation level


# ─────────────────────────────────────────────────────────────────────────────
#  Controller
# ─────────────────────────────────────────────────────────────────────────────


class ActiveInferenceDynamic(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._lock = threading.Lock()

        # ── Network graph ─────────────────────────────────────────────────────
        self._graph = nx.Graph()  # switch-to-switch topology graph

        # ── Dataplane state ───────────────────────────────────────────────────
        self._datapaths = {}  # dpid (int) -> datapath object

        # ── Host learning ─────────────────────────────────────────────────────
        self._ip_to_mac = {}  # ip  -> mac  (from ARP)
        self._mac_to_ip = {}  # mac -> ip
        self._host_location = {}  # ip  -> (dpid, port)   (switch/port)

        # ── Switch-to-switch port map ─────────────────────────────────────────
        # _sw_port[(src_dpid, dst_dpid)] = out_port on src to reach dst
        self._sw_port = {}

        # ── Per-link utilisation ──────────────────────────────────────────────
        # _link_util[(src_dpid, dst_dpid)] = {"rate_mbps": ..., "util": ..., "capacity_mbps": ...}
        self._link_util = {}

        # ── Per-port byte / time accumulators ────────────────────────────────
        self._last_bytes = defaultdict(int)  # (dpid, port_no) -> total bytes
        self._last_time = {}  # (dpid, port_no) -> timestamp

        # ── Active flows ──────────────────────────────────────────────────────
        # _flows[(src_ip, dst_ip)] = {
        #   "path": [dpid, ...],      # list of switch dpids in order
        #   "path_idx": int,          # index into _flow_candidates
        #   "beliefs": {idx: PathBelief},
        #   "load_estimate": float,
        # }
        self._flows = {}

        # ── Candidate paths cache ─────────────────────────────────────────────
        # _flow_candidates[(src_ip, dst_ip)] = [[dpid,...], ...]
        self._flow_candidates = {}

        # Start monitor loop
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # ─────────────────────────────────────────────────────────────────────────
    #  OpenFlow helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _add_flow(self, dp, priority, match, actions, idle_timeout=0, hard_timeout=0):
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

    def _del_flow(self, dp, match, priority=20):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser
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

    # ─────────────────────────────────────────────────────────────────────────
    #  Switch connected
    # ─────────────────────────────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        with self._lock:
            self._datapaths[dp.id] = dp
            # Ensure the switch node exists in the graph
            if not self._graph.has_node(dp.id):
                self._graph.add_node(dp.id, type="switch")

        # Install table-miss: send all unknown packets to controller
        self._add_flow(
            dp,
            0,
            parser.OFPMatch(),
            [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)],
        )
        self.logger.info("Switch s%d connected", dp.id)

    # ─────────────────────────────────────────────────────────────────────────
    #  Topology discovery events
    # ─────────────────────────────────────────────────────────────────────────

    @set_ev_cls(topo_event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        """Called when a switch registers with the topology discovery module."""
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
        self.logger.info("Topology: switch s%d left", dpid)

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        """Called when a bidirectional link is discovered."""
        src = ev.link.src  # Port object: .dpid, .port_no
        dst = ev.link.dst
        with self._lock:
            # Update switch-to-switch port map
            self._sw_port[(src.dpid, dst.dpid)] = src.port_no
            self._sw_port[(dst.dpid, src.dpid)] = dst.port_no

            # Add edge to graph with default capacity
            if not self._graph.has_edge(src.dpid, dst.dpid):
                self._graph.add_edge(
                    src.dpid,
                    dst.dpid,
                    src_port=src.port_no,
                    dst_port=dst.port_no,
                    capacity_mbps=DEFAULT_LINK_BW_MBPS,
                    weight=1,
                )

            # Initialise utilisation record
            key = (src.dpid, dst.dpid)
            if key not in self._link_util:
                self._link_util[key] = {
                    "rate_mbps": 0.0,
                    "util": 0.0,
                    "capacity_mbps": DEFAULT_LINK_BW_MBPS,
                }
            key2 = (dst.dpid, src.dpid)
            if key2 not in self._link_util:
                self._link_util[key2] = {
                    "rate_mbps": 0.0,
                    "util": 0.0,
                    "capacity_mbps": DEFAULT_LINK_BW_MBPS,
                }

            # Invalidate cached candidate paths — topology changed
            self._flow_candidates.clear()

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
        with self._lock:
            self._sw_port.pop((src.dpid, dst.dpid), None)
            self._sw_port.pop((dst.dpid, src.dpid), None)
            if self._graph.has_edge(src.dpid, dst.dpid):
                self._graph.remove_edge(src.dpid, dst.dpid)
            self._flow_candidates.clear()
        self.logger.info("Topology: link s%d <-> s%d removed", src.dpid, dst.dpid)

    # ─────────────────────────────────────────────────────────────────────────
    #  Packet-in: host learning + forwarding
    # ─────────────────────────────────────────────────────────────────────────

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

        src_mac = eth.src

        # ── ARP learning ──────────────────────────────────────────────────────
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            src_ip = arp_pkt.src_ip
            self._learn_host(src_ip, src_mac, dpid, in_port)

            # Flood ARP so all hosts learn MAC addresses
            self._pkt_out(
                dp,
                in_port,
                [parser.OFPActionOutput(ofproto.OFPP_FLOOD)],
                msg.data,
            )
            return

        # ── IPv4 forwarding ───────────────────────────────────────────────────
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            src_ip = ip_pkt.src
            dst_ip = ip_pkt.dst
            self._learn_host(src_ip, src_mac, dpid, in_port)

            out_port = self._get_out_port(dpid, dst_ip)
            if out_port is not None:
                self._pkt_out(
                    dp,
                    in_port,
                    [parser.OFPActionOutput(out_port)],
                    msg.data,
                )
            else:
                # No known route; flood as last resort
                self._pkt_out(
                    dp,
                    in_port,
                    [parser.OFPActionOutput(ofproto.OFPP_FLOOD)],
                    msg.data,
                )
                # Try to install flows if we know src/dst
                if src_ip in self._host_location and dst_ip in self._host_location:
                    self._install_flow_pair(src_ip, dst_ip)

    def _learn_host(self, ip: str, mac: str, dpid: int, port: int):
        """Record IP->MAC and IP->switch/port mappings."""
        with self._lock:
            if ip not in self._ip_to_mac:
                self._ip_to_mac[ip] = mac
                self._mac_to_ip[mac] = ip
                self.logger.info(
                    "Learned host %s MAC=%s at s%d-eth%d", ip, mac, dpid, port
                )
            if ip not in self._host_location:
                self._host_location[ip] = (dpid, port)
                self.logger.info(
                    "Learned host %s location: s%d port %d", ip, dpid, port
                )
                # Trigger flow installation for all known pairs involving this host
                self._trigger_flow_install(ip)

    def _trigger_flow_install(self, new_ip: str):
        """Install flows for all pairs where both endpoints are known."""
        known = list(self._host_location.keys())
        for other_ip in known:
            if other_ip == new_ip:
                continue
            self._install_flow_pair(new_ip, other_ip)
            self._install_flow_pair(other_ip, new_ip)

    # ─────────────────────────────────────────────────────────────────────────
    #  Path computation
    # ─────────────────────────────────────────────────────────────────────────

    def _get_candidate_paths(self, src_ip: str, dst_ip: str) -> list:
        """
        Return up to MAX_CANDIDATE_PATHS simple paths between src and dst switches.
        Paths are lists of switch dpids (int).
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
                # Same switch — no inter-switch path needed
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
        Return the outgoing port on switch `dpid` to reach host `dst_ip`,
        using the currently active flow entry.
        """
        with self._lock:
            # Direct host port (destination is on this switch)
            if dst_ip in self._host_location:
                dst_dpid, dst_port = self._host_location[dst_ip]
                if dst_dpid == dpid:
                    return dst_port

            # Consult active flow entry
            for (s_ip, d_ip), flow_info in self._flows.items():
                if d_ip != dst_ip:
                    continue
                path = flow_info["path"]
                if dpid in path:
                    idx = path.index(dpid)
                    if idx + 1 < len(path):
                        next_dpid = path[idx + 1]
                        return self._sw_port.get((dpid, next_dpid))
                    # Last switch in path — deliver to host
                    if dst_ip in self._host_location:
                        dst_dpid, dst_port = self._host_location[dst_ip]
                        if dst_dpid == dpid:
                            return dst_port
        return None

    # ─────────────────────────────────────────────────────────────────────────
    #  Flow installation
    # ─────────────────────────────────────────────────────────────────────────

    def _install_flow_pair(self, src_ip: str, dst_ip: str):
        """
        Install flows for src_ip -> dst_ip (one direction).
        Selects path via Active Inference; installs OpenFlow rules along it.
        """
        candidates = self._get_candidate_paths(src_ip, dst_ip)
        if not candidates:
            return

        key = (src_ip, dst_ip)

        with self._lock:
            flow = self._flows.get(key)
            if flow is None:
                # New flow — initialise PathBelief for each candidate
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
                # Resize beliefs dict if topology changed (more/fewer paths)
                if len(beliefs) != len(candidates):
                    new_beliefs = {}
                    for i in range(len(candidates)):
                        new_beliefs[i] = beliefs.get(i, PathBelief())
                    beliefs = new_beliefs
                    flow["beliefs"] = beliefs

        # Select best path via EFE
        best_idx = self._select_best_path(
            key, candidates, beliefs, active_idx, flow.get("load_estimate", 0.0)
        )

        with self._lock:
            flow = self._flows[key]
            old_idx = flow["path_idx"]

        if best_idx != old_idx:
            self.logger.info(
                "Flow %s->%s: rerouting path%d -> path%d",
                src_ip,
                dst_ip,
                old_idx,
                best_idx,
            )
            with self._lock:
                flow["path_idx"] = best_idx
                flow["path"] = candidates[best_idx]

        self._push_flow_rules(src_ip, dst_ip, candidates[best_idx])

    def _push_flow_rules(self, src_ip: str, dst_ip: str, path: list):
        """
        Install OpenFlow rules along `path` (list of switch dpids) for src->dst.
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
                if dpid == dst_dpid:
                    out_port = dst_port
                else:
                    out_port = None

            if out_port is None:
                self.logger.warning(
                    "No out_port for s%d on path to %s — skipping", dpid, dst_ip
                )
                continue

            # Delete stale high-priority rules for this exact match first
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst_ip)
            self._del_flow(dp, match, priority=20)

            # Install new rule
            self._add_flow(
                dp,
                20,
                parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst_ip),
                [parser.OFPActionOutput(out_port)],
            )

        self.logger.info(
            "Installed flow %s->%s via path: %s",
            src_ip,
            dst_ip,
            " -> ".join(f"s{d}" for d in path),
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  Active Inference — policy selection
    # ─────────────────────────────────────────────────────────────────────────

    def _select_best_path(
        self,
        flow_key: tuple,
        candidates: list,
        beliefs: dict,
        active_idx: int,
        load_estimate: float,
    ) -> int:
        """
        Compute EFE for all candidate paths and return the best index.
        Uses softmax selection with hysteresis on the active path.
        """
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

        # Numerically stable softmax
        g_min = min(G_values.values())
        exp_vals = {
            i: math.exp(-EFE_TEMPERATURE * (G_values[i] - g_min)) for i in G_values
        }
        Z = sum(exp_vals.values())
        probs = {i: exp_vals[i] / Z for i in exp_vals}

        best_idx = max(probs, key=lambda i: probs[i])

        # Hysteresis: only reroute if meaningful improvement
        if best_idx != active_idx:
            improvement = G_values[active_idx] - G_values[best_idx]
            p_reroute = probs[best_idx]
            if (
                improvement < REROUTE_MIN_IMPROVEMENT
                or p_reroute < SWITCH_PROB_THRESHOLD
            ):
                best_idx = active_idx
                self.logger.info(
                    "Flow %s: hysteresis hold on path%d (improvement=%.4f, P=%.3f)",
                    flow_key,
                    active_idx,
                    improvement,
                    p_reroute,
                )

        return best_idx

    def _path_max_util(self, path: list) -> float:
        """
        Return the maximum link utilisation along `path` (bottleneck link).
        Path is a list of switch dpids.
        """
        max_util = 0.0
        with self._lock:
            for i in range(len(path) - 1):
                key = (path[i], path[i + 1])
                info = self._link_util.get(key, {})
                max_util = max(max_util, info.get("util", 0.0))
        return max_util

    # ─────────────────────────────────────────────────────────────────────────
    #  Monitor loop — port stats
    # ─────────────────────────────────────────────────────────────────────────

    def _monitor_loop(self):
        hub.sleep(4)  # wait for topology to stabilise
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
        """
        1. Compute per-port utilisation for all switch ports.
        2. Update link_util store keyed by (src_dpid, dst_dpid).
        3. Update PathBelief for all active flows.
        4. Re-run EFE and reroute if beneficial.
        5. Write state.json.
        """
        dpid = ev.msg.datapath.id
        now = time.time()

        # ── Compute per-port rate ─────────────────────────────────────────────
        port_rates = {}
        for stat in ev.msg.body:
            port_no = stat.port_no
            if port_no >= 0xFFFFFFF0:  # skip special OFPP_ ports
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

        # ── Update link_util using sw_port map ────────────────────────────────
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
                # Update NetworkX edge weight inversely proportional to remaining capacity
                if self._graph.has_edge(src, dst):
                    self._graph[src][dst]["weight"] = max(0.01, util)

        # ── Update PathBeliefs and re-evaluate flows ──────────────────────────
        self._run_inference_cycle()

    def _run_inference_cycle(self):
        """
        For each tracked flow:
        1. Measure per-path utilisation from link_util (max link = bottleneck).
        2. Update PathBelief for active path (reinforce confidence) and others (decay).
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
            active_path = flow["path"]

            # ── Update beliefs with measured utilisation ──────────────────────
            for i, path in enumerate(candidates):
                util = self._path_max_util(path)
                beliefs[i].update(util)

            # Confidence: active path gains certainty, others lose it
            for i in range(len(candidates)):
                if i == active_idx:
                    beliefs[i].reinforce_confidence()
                else:
                    beliefs[i].decay_confidence()

            # ── Estimate flow's contribution to active path utilisation ────────
            # Rough heuristic: active path util minus min idle path util
            active_util = beliefs[active_idx].mu
            idle_utils = [
                beliefs[i].mu for i in range(len(candidates)) if i != active_idx
            ]
            min_idle = min(idle_utils) if idle_utils else 0.0
            load_est = max(0.0, active_util - min_idle)
            load_est = 0.6 * flow.get("load_estimate", 0.0) + 0.4 * load_est
            with self._lock:
                flow["load_estimate"] = load_est

            # ── Select best path via EFE ───────────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────────
    #  State export
    # ─────────────────────────────────────────────────────────────────────────

    def _write_state(self, event: str = ""):
        """
        Atomically write the current network state to STATE_JSON_PATH.
        Uses temp-file + os.replace so the visualiser never reads a partial file.

        Schema:
        {
          "timestamp": "...",
          "nodes": [{"id": "s1", "type": "switch"}, {"id": "h1", "type": "host", "ip": "..."}],
          "links": [{"src": "s1", "dst": "s2", "src_port": 2, "dst_port": 1,
                     "rate_mbps": 4.52, "util": 0.452, "capacity_mbps": 10}],
          "flows": [{"src_ip": "...", "dst_ip": "...", "path": ["s1","s3","s4"],
                     "G": 0.42, "rerouted": true}],
          "event": "..."
        }
        """
        with self._lock:
            # Nodes
            nodes = []
            for dpid in self._graph.nodes:
                nodes.append({"id": f"s{dpid}", "type": "switch"})
            for ip, (dpid, port) in self._host_location.items():
                nodes.append({"id": f"h_{ip}", "type": "host", "ip": ip})

            # Links
            links = []
            seen_links = set()
            for (src, dst), port in self._sw_port.items():
                if (dst, src) in seen_links:
                    continue
                seen_links.add((src, dst))
                rev_port = self._sw_port.get((dst, src), 0)
                util_info = self._link_util.get((src, dst), {})
                links.append(
                    {
                        "src": f"s{src}",
                        "dst": f"s{dst}",
                        "src_port": port,
                        "dst_port": rev_port,
                        "rate_mbps": util_info.get("rate_mbps", 0.0),
                        "util": util_info.get("util", 0.0),
                        "capacity_mbps": util_info.get(
                            "capacity_mbps", DEFAULT_LINK_BW_MBPS
                        ),
                    }
                )

            # Flows
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
                flows_out.append(
                    {
                        "src_ip": src_ip,
                        "dst_ip": dst_ip,
                        "path": path_labels,
                        "G": round(G, 5),
                        "rerouted": "REROUTED" in event and src_ip in event,
                    }
                )

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
