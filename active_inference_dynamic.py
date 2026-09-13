"""
active_inference_dynamic.py  —  Topology-Independent Ryu SDN Controller
=========================================================================

This file contains only the RyuApp class.  All logic is delegated to:

  ai/belief.py            PathBelief  (+ serialize/deserialize snapshot)
  ai/policy.py            compute_efe_for_path, select_best_path,
                          compute_multipath_weights
  sdn/constants.py        all constants  (incl. ENABLE_MODEL_TRADING,
                          ENABLE_AUDIT_LEDGER, CONTROLLER_DOMAINS, CIU)
  sdn/topology_manager.py graph, switches, links, trunk ports, paths,
                          per-link loss_fraction (for CIU)
  sdn/host_manager.py     host IP/MAC/location learning
  sdn/flow_manager.py     add/delete/install flow rules, PacketOut,
                          install_multipath_flows (SELECT groups)
  sdn/state_writer.py     atomic state.json export (+ optional ledger)
  utils/ip_utils.py       IP address classification
  blockchain/*            DID, ledger, smart contracts, model store,
                          7-step trading protocol, audit logging

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

Blockchain layer (Section III / Fig. 2)
---------------------------------------
With ENABLE_MODEL_TRADING and ENABLE_AUDIT_LEDGER (defaults: both True),
two logical ControllerIdentity instances (alpha managing {s1, s2}, beta
managing {s3, s4}) live inside this single RyuApp:

  * On cold-start of a new flow, the owning controller tries to import a
    peer's trained PathBelief snapshot via the 7-step trade protocol
    (blockchain.trading.trade_model) before falling back to the default
    PathBelief() prior.
  * Each inference tick (per flow), the owning controller publishes its
    own current belief snapshot to the model store (throttled), so the
    peer has something to trade for.
  * Each inference tick (per flow), a `metrics_log` block is appended to
    the shared ledger with free energy, load, link utils, link losses,
    and CIU (Eq. 6).
  * state.json gains an additive "ledger" key — existing readers that
    ignore unknown keys keep working unchanged.

With both flags set to False, controller behaviour is unchanged from the
pre-blockchain codebase (regression-safety bar).

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

from ai.belief import PathBelief, serialize_belief_snapshot, deserialize_belief_snapshot
from ai.policy import compute_multipath_weights, compute_efe_for_path, select_best_path
from sdn import flow_manager as fm
from sdn import topology_spec
from sdn.constants import (
    ARP_CACHE_TTL,
    CONTROLLER_DID_LABELS,
    ENABLE_AUDIT_LEDGER,
    ENABLE_MODEL_TRADING,
    ETH_TYPE_LLDP,
    IPFS_NODE_COUNT,
    LINK_FLAP_GRACE_SEC,
    MODEL_PUBLISH_INTERVAL_TICKS,
    MODEL_SHARD_COUNT,
    MULTIPATH_CONGESTION_THRESHOLD,
    POLL_INTERVAL,
    PREFERRED_UTIL,
    STATE_JSON_PATH,
)
from sdn.host_manager import HostManager
from sdn.state_writer import write_state
from sdn.topology_manager import TopologyManager
from utils.ip_utils import is_valid_host_ip

# Blockchain layer imports — guarded so a missing `cryptography` package
# or a flipped-off flag does not crash the controller on import. The
# actual instantiation happens in __init__ only when both flags are on.
try:
    from blockchain.audit import compute_ciu, log_cycle
    from blockchain.contracts import ModelTradingContract
    from blockchain.identity import ControllerIdentity, DIDRegistry
    from blockchain.ledger import Ledger
    from blockchain.model_store import ModelStore
    from blockchain.trading import register_did, trade_model

    _BLOCKCHAIN_AVAILABLE = True
except Exception as _bc_err:  # pragma: no cover — defensive
    _BLOCKCHAIN_AVAILABLE = False
    _BLOCKCHAIN_IMPORT_ERROR = _bc_err


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
        #   "owning_ctrl":  int,             controller id (1 or 2)
        #   "ticks":        int,             inference tick counter (for throttling)
        # }
        self._flows = {}

        # Per-port byte / time accumulators
        self._last_bytes = defaultdict(int)  # (dpid, port_no) -> bytes
        self._last_time = {}  # (dpid, port_no) -> timestamp

        # Per-port packet / drop counters (for CIU packet-loss term, Eq. 6).
        self._last_pkts = defaultdict(int)  # (dpid, port_no) -> rx+tx packets
        self._last_drops = defaultdict(int)  # (dpid, port_no) -> rx+tx dropped

        # Per-flow byte accumulators for direct load measurement
        # key: (src_ip, dst_ip) -> {"bytes": int, "time": float, "rate_mbps": float}
        self._flow_bytes = {}

        # ARP duplicate-suppression cache
        # key: (dpid, in_port, src_mac, dst_ip, opcode) -> expiry timestamp
        self._arp_seen = {}

        # Pending link removals, for flap debouncing.
        # key: (src_dpid, dst_dpid) normalised low->high -> hub.GreenThread
        self._pending_link_removal = {}

        # ── Blockchain layer bootstrap ─────────────────────────────────────
        # Both flags default to True; if either is off, or the `cryptography`
        # package is missing, the entire blockchain subsystem is skipped
        # and the controller behaves identically to the pre-blockchain code.
        self._ledger = None
        self._registry = None
        self._model_store = None
        self._contract = None
        self._controllers = {}  # controller_id (int) -> ControllerIdentity
        self._bc_enabled = (
            ENABLE_MODEL_TRADING or ENABLE_AUDIT_LEDGER
        ) and _BLOCKCHAIN_AVAILABLE

        if not _BLOCKCHAIN_AVAILABLE and (ENABLE_MODEL_TRADING or ENABLE_AUDIT_LEDGER):
            self.logger.warning(
                "Blockchain layer requested but unavailable (%s); "
                "running in regression mode.",
                getattr(_BLOCKCHAIN_IMPORT_ERROR, "__name__", "import error"),
            )
            self._bc_enabled = False

        # dpid -> controller id, recomputed dynamically as switches come
        # and go (see _refresh_controller_domains) instead of trusting the
        # static CONTROLLER_DOMAINS = {1: {1,2}, 2: {3,4}} shape, which is
        # stale for any topology whose dpids aren't exactly {1,2,3,4}
        # (task doc §2.1).
        self._dpid_to_cid = {}
        self._published_model_ids = {}

        if self._bc_enabled:
            self._ledger = Ledger()
            self._registry = DIDRegistry()
            self._model_store = ModelStore(node_count=IPFS_NODE_COUNT)
            self._contract = ModelTradingContract(self._ledger)
            # Controller identities (and _dpid_to_cid) are (re)built the
            # first time switches are discovered — see
            # _refresh_controller_domains(), invoked from
            # switch_enter_handler/switch_leave_handler.

        # ── Topology spec (bandwidth source of truth) ───────────────────────
        # Read once at startup so a manually-launched controller (fallback
        # path, no orchestrator) still gets real link capacities if a spec
        # file happens to be present; re-checked every monitor tick so an
        # "Apply topology" from the GUI (which rewrites this file) is
        # picked up without restarting the controller (task doc §3 — the
        # controller is topology-independent and doesn't need a restart).
        self._spec_path = os.environ.get(
            "SDN_TOPOLOGY_SPEC", topology_spec.DEFAULT_SPEC_PATH
        )
        self._spec_mtime = None
        self._reload_topology_spec(initial=True)

        # Monitor loop
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # =========================================================================
    #  Topology spec (bandwidth source of truth) + dynamic controller domains
    # =========================================================================

    def _reload_topology_spec(self, initial: bool = False) -> None:
        """
        Re-read the topology spec file (if it changed) and push its
        switch<->switch link bandwidths into TopologyManager, so
        utilisation/EFE/congestion math is computed against the real
        configured capacity instead of DEFAULT_LINK_BW_MBPS (task §2.2).
        Cheap no-op when the file hasn't changed since last check.
        """
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

    def _refresh_controller_domains(self) -> None:
        """
        Recompute the controller-id <-> dpid split from whatever switches
        are *currently* discovered (TopologyManager.compute_controller_domains)
        and keep self._dpid_to_cid / self._controllers[*].managed_dpids in
        sync with it. Called on switch enter/leave. A no-op unless the
        blockchain layer is enabled — _dpid_to_cid still needs to exist
        either way so _owning_controller_id() never KeyErrors.
        """
        domains = self._topo.compute_controller_domains()

        dpid_to_cid = {}
        for cid, dpids in domains.items():
            for d in dpids:
                dpid_to_cid[d] = cid
        self._dpid_to_cid = dpid_to_cid

        if not self._bc_enabled:
            return

        for cid, dpids in domains.items():
            existing = self._controllers.get(cid)
            if existing is not None:
                # ControllerIdentity keeps its DID/keys; only the managed
                # dpid set needs updating as the topology changes.
                existing.managed_dpids = set(dpids)
                continue
            did = CONTROLLER_DID_LABELS.get(cid, f"c{cid}")
            ident = ControllerIdentity(did=did, managed_dpids=set(dpids))
            self._controllers[cid] = ident
            try:
                register_did(ident, self._registry, self._ledger)
                self.logger.info(
                    "Blockchain: registered controller %s (cid=%d) "
                    "managing dpids %s",
                    did,
                    cid,
                    sorted(dpids),
                )
            except Exception as exc:  # pragma: no cover — defensive
                self.logger.warning(
                    "Blockchain: DID registration failed for %s: %s", did, exc
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
    #  Topology discovery events
    # =========================================================================

    @set_ev_cls(topo_event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        dpid = ev.switch.dp.id
        self._topo.add_switch(dpid, ev.switch.dp)
        self._refresh_controller_domains()
        self.logger.info("Topology: switch s%d discovered", dpid)

    @set_ev_cls(topo_event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        self._topo.remove_switch(dpid)
        self._refresh_controller_domains()
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
    #  ARP handler  (loop safe, unicast when possible)
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
    #  Blockchain layer — controller ownership & model trading
    # =========================================================================

    def _owning_controller_id(self, src_ip: str) -> int:
        """
        Return the controller id (1 or 2) that owns the flow (src_ip, *),
        determined by which controller manages the switch hosting src_ip.
        Returns 0 if no controller manages that dpid (e.g. topology not
        yet discovered, or dpid outside both domains).
        """
        loc = self._hosts.get_location(src_ip)
        if loc is None:
            return 0
        dpid, _ = loc
        return self._dpid_to_cid.get(dpid, 0)

    def _try_import_belief_from_peer(
        self, owning_cid: int, flow_key: tuple, candidates: list
    ) -> dict:
        """
        Attempt to fetch a peer controller's trained PathBelief snapshot
        for this flow via the 7-step trade protocol.

        Returns a {idx: PathBelief} dict on success, or an empty dict on
        any failure (caller falls back to default PathBelief() priors).
        """
        if not (self._bc_enabled and ENABLE_MODEL_TRADING):
            return {}
        if owning_cid == 0 or len(self._controllers) < 2:
            return {}

        owning = self._controllers.get(owning_cid)
        if owning is None:
            return {}

        # Find the peer controller (the other one).
        peer_cid = next((cid for cid in self._controllers if cid != owning_cid), None)
        if peer_cid is None:
            return {}
        peer = self._controllers[peer_cid]

        src_ip, dst_ip = flow_key

        # Look for any model the peer has published for this flow.
        # The convention is: peer publishes under a deterministic model_id
        # keyed by (peer_did, src_ip, dst_ip, path_idx). Try path 0 first.
        for path_idx in range(min(len(candidates), 2)):
            candidate_model_id = f"model_{peer.did}_{src_ip}_{dst_ip}_p{path_idx}"
            try:
                token = trade_model(
                    requesting=owning,
                    providing=peer,
                    model_id=candidate_model_id,
                    registry=self._registry,
                    store=self._model_store,
                    contract=self._contract,
                    ledger=self._ledger,
                    logger=self.logger,
                )
            except Exception as exc:
                self.logger.debug(
                    "Blockchain: trade_model raised for %s: %s",
                    candidate_model_id,
                    exc,
                )
                continue

            if token is None:
                continue

            retrieved = self._model_store.retrieve_model(candidate_model_id)
            if not retrieved:
                continue

            try:
                imported = deserialize_belief_snapshot(retrieved)
            except Exception as exc:
                self.logger.debug(
                    "Blockchain: snapshot deserialization failed for %s: %s",
                    candidate_model_id,
                    exc,
                )
                continue

            # Align imported beliefs to current candidate count.
            aligned = {i: imported.get(i, PathBelief()) for i in range(len(candidates))}
            self.logger.info(
                "Blockchain: %s imported belief snapshot from %s for %s->%s "
                "(model=%s)",
                owning.did,
                peer.did,
                src_ip,
                dst_ip,
                candidate_model_id,
            )
            return aligned

        return {}

    def _publish_belief_snapshot(
        self,
        owning_cid: int,
        flow_key: tuple,
        candidates: list,
        beliefs: dict,
        active_idx: int,
    ) -> None:
        """
        Serialize this controller's current belief snapshot for `flow_key`
        and store it in the model store so the peer can trade for it later.

        Throttled by MODEL_PUBLISH_INTERVAL_TICKS — call only when the
        flow's tick counter hits a multiple of that interval.
        """
        if not (self._bc_enabled and ENABLE_MODEL_TRADING):
            return
        if owning_cid == 0:
            return
        owning = self._controllers.get(owning_cid)
        if owning is None:
            return

        src_ip, dst_ip = flow_key
        model_id = f"model_{owning.did}_{src_ip}_{dst_ip}_p{active_idx}"

        try:
            snapshot_json = serialize_belief_snapshot(
                beliefs,
                flow_key=flow_key,
                efe_hyperparameters={
                    "EFE_TEMPERATURE": 8.0,
                    "PREFERRED_UTIL": PREFERRED_UTIL,
                    "MULTIPATH_CONGESTION_THRESHOLD": MULTIPATH_CONGESTION_THRESHOLD,
                },
            )
            payload_bytes = snapshot_json.encode("utf-8")
            shard_hashes = self._model_store.store_model(
                model_id,
                payload_bytes,
                num_shards=MODEL_SHARD_COUNT,
            )

            # Register or update the contract entry.
            existing = self._contract.get_model_descriptions(model_id)
            if existing is None:
                self._contract.create_model(
                    owner=owning.did,
                    model_descriptions={
                        "owner": owning.did,
                        "hash": shard_hashes[0] if shard_hashes else "",
                        "timestamp": time.time(),
                        "reputation": 0.5,
                    },
                )
            else:
                self._contract.update_model_descriptions(
                    model_id, shard_hashes[0] if shard_hashes else ""
                )

            self._published_model_ids[(owning_cid, src_ip, dst_ip, active_idx)] = (
                model_id
            )
        except Exception as exc:
            self.logger.debug(
                "Blockchain: belief publish failed for %s->%s: %s",
                src_ip,
                dst_ip,
                exc,
            )

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

        # Determine the owning controller for the forward flow.
        owning_cid = self._owning_controller_id(src_ip)

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
                    beliefs = None
                    # Only the forward flow's owner attempts a model trade
                    # on cold-start; the reverse flow is the same controller
                    # pair so reusing the imported beliefs is appropriate.
                    if key == fwd_key and self._bc_enabled and ENABLE_MODEL_TRADING:
                        imported = self._try_import_belief_from_peer(
                            owning_cid, key, candidates
                        )
                        if imported:
                            beliefs = imported

                    if beliefs is None:
                        beliefs = {i: PathBelief() for i in range(len(candidates))}

                    seed_load = self._topo.path_max_util(candidates[0])
                    self._flows[key] = {
                        "path": candidates[0],
                        "path_idx": 0,
                        "beliefs": beliefs,
                        "load_estimate": seed_load,
                        "multipath": False,
                        "owning_ctrl": owning_cid,
                        "ticks": 0,
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
                    "owning_ctrl": 0,
                    "ticks": 0,
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
        """
        Receive per-port byte counters and compute per-port rate_mbps +
        per-port packet-loss fraction (delta_dropped / delta_packets).

        The loss fraction feeds the CIU packet-loss term (Eq. 6) via
        TopologyManager.update_link_drops().
        """
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

            # ── Bytes → rate_mbps (existing behaviour) ──────────────────────
            total_bytes = stat.rx_bytes + stat.tx_bytes
            last_b = self._last_bytes.get(key, total_bytes)
            last_t = self._last_time.get(key, now)
            dt = max(now - last_t, 0.001)
            delta_bytes = max(0, total_bytes - last_b)
            rate_mbps = (delta_bytes * 8) / 1e6 / dt
            self._last_bytes[key] = total_bytes
            self._last_time[key] = now
            port_rates[port_no] = rate_mbps

            # ── Drops → loss_fraction (new, parallel to rate_mbps) ──────────
            total_pkts = stat.rx_packets + stat.tx_packets
            total_drops = stat.rx_dropped + stat.tx_dropped
            last_p = self._last_pkts.get(key, total_pkts)
            last_d = self._last_drops.get(key, total_drops)
            pkt_delta = max(0, total_pkts - last_p)
            drop_delta = max(0, total_drops - last_d)
            if pkt_delta > 0:
                loss_fraction = drop_delta / pkt_delta
            else:
                # No packet delta this interval — keep previous loss value
                # rather than fabricating a 0 (which would erase history).
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

        self._run_inference_cycle()

    def _run_inference_cycle(self):
        """
        Update PathBeliefs for all flows, re-run EFE, reroute or rebalance
        multipath weights if beneficial, then export state.json.

        With ENABLE_AUDIT_LEDGER on, appends one `metrics_log` block per
        flow per tick to the shared ledger.
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

                flow["ticks"] = flow.get("ticks", 0) + 1
                owning_cid = flow.get("owning_ctrl", 0)

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

                # ── Audit + publish (throttled) ─────────────────────────────
                active_path = candidates[active_idx]
                self._audit_and_publish(
                    flow,
                    flow_key,
                    owning_cid,
                    candidates,
                    active_path,
                    active_idx,
                    beliefs,
                    load_est,
                    fwd_weights,
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

                    self._audit_and_publish(
                        flow,
                        flow_key,
                        owning_cid,
                        candidates,
                        candidates[best_idx],
                        best_idx,
                        beliefs,
                        load_est,
                        None,
                    )
                else:
                    with self._lock:
                        flow["multipath"] = False
                    events.append(
                        f"Held {src_ip}->{dst_ip} on path{active_idx} "
                        f"(util={beliefs[active_idx].mu:.2f})"
                    )

                    self._audit_and_publish(
                        flow,
                        flow_key,
                        owning_cid,
                        candidates,
                        candidates[active_idx],
                        active_idx,
                        beliefs,
                        load_est,
                        None,
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
            ledger=self._ledger if self._bc_enabled else None,
        )

    # -------------------------------------------------------------------------
    #  Audit-log + belief-publish helper (called once per flow per tick)
    # -------------------------------------------------------------------------

    def _audit_and_publish(
        self,
        flow,
        flow_key,
        owning_cid,
        candidates,
        active_path,
        active_idx,
        beliefs,
        load_est,
        multipath_weights,
    ) -> None:
        """
        Append a metrics_log block to the shared ledger (when
        ENABLE_AUDIT_LEDGER is on) and publish the owning controller's
        belief snapshot to the model store (when ENABLE_MODEL_TRADING is
        on and the per-flow tick counter hits the throttle interval).

        CIU (Eq. 6) is computed from the bottleneck load and bottleneck
        loss along the active path. If packet-loss measurement is
        unavailable (e.g. the switch didn't report rx_dropped), CIU is
        logged as None rather than fabricated.
        """
        if not self._bc_enabled:
            return

        src_ip, dst_ip = flow_key

        # ── Audit log ──────────────────────────────────────────────────────
        if ENABLE_AUDIT_LEDGER and self._ledger is not None:
            link_utils = self._topo.path_link_utils(active_path)
            link_losses = self._topo.path_link_losses(active_path)
            try:
                # Free energy of the active path under the "STAY" action.
                G = compute_efe_for_path(
                    path_idx=active_idx,
                    active_idx=active_idx,
                    beliefs=beliefs,
                    load_delta=load_est,
                )
            except Exception:
                G = 0.0

            ciu = None
            try:
                bottleneck_load = max(link_utils.values()) if link_utils else load_est
                bottleneck_loss = max(link_losses.values()) if link_losses else 0.0
                ciu = compute_ciu(bottleneck_load, bottleneck_loss)
            except Exception as exc:
                self.logger.debug("CIU computation failed: %s", exc)

            owner_label = (
                self._controllers.get(owning_cid, None)
                and self._controllers[owning_cid].did
            ) or f"c{owning_cid}"

            try:
                log_cycle(
                    ledger=self._ledger,
                    controller_id=owner_label,
                    src_ip=src_ip,
                    dst_ip=dst_ip,
                    path=active_path,
                    G=G,
                    load_estimate=load_est,
                    link_utils=link_utils,
                    link_losses=link_losses,
                    ciu=ciu,
                    multipath_weights=multipath_weights,
                )
            except Exception as exc:
                self.logger.debug("audit log_cycle failed: %s", exc)

        # ── Belief publish (throttled) ─────────────────────────────────────
        if (
            ENABLE_MODEL_TRADING
            and self._model_store is not None
            and owning_cid != 0
            and flow.get("ticks", 0) % MODEL_PUBLISH_INTERVAL_TICKS == 0
        ):
            try:
                self._publish_belief_snapshot(
                    owning_cid,
                    flow_key,
                    candidates,
                    beliefs,
                    active_idx,
                )
            except Exception as exc:
                self.logger.debug("belief publish failed: %s", exc)
