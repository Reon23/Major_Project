"""
sdn/constants.py — All project-wide constants.
"""

# ── Ethertypes ───────────────────────────────────────────────────────────────
ETH_TYPE_IP = 0x0800
ETH_TYPE_ARP = 0x0806
ETH_TYPE_LLDP = 0x88CC

# ── ARP opcodes ──────────────────────────────────────────────────────────────
ARP_REQUEST = 1
ARP_REPLY = 2

# ── OpenFlow priorities ──────────────────────────────────────────────────────
PRIORITY_TABLE_MISS = 0
PRIORITY_FLOW = 20  # forwarding rules
PRIORITY_ARP_IN = 10  # not currently installed as a rule; kept for reference

# ── Flow timeouts ─────────────────────────────────────────────────────────────
FLOW_IDLE_TIMEOUT = 30  # seconds; 0 = permanent
FLOW_HARD_TIMEOUT = 0  # no hard timeout

# ── Link utilisation ───────────────────────────────────────────────────────
DEFAULT_LINK_BW_MBPS = 10.0
MAX_CANDIDATE_PATHS = 4

# ── Link-flap debouncing ──────────────────────────────────────────────────────
# Under heavy data-plane congestion, LLDP discovery probes can be queued behind
# saturated traffic and arrive late, causing ryu.topology.switches to fire a
# spurious EventLinkDelete followed shortly by EventLinkAdd for the same link
# (the link was never actually down). LINK_FLAP_GRACE_SEC is how long we wait
# after an EventLinkDelete before treating the link as genuinely removed; if
# EventLinkAdd for the same link arrives within this window, the removal is
# cancelled and nothing changes (no flow flush, no topology edit).
LINK_FLAP_GRACE_SEC = 3.0

# ── Monitoring ────────────────────────────────────────────────────────────────
POLL_INTERVAL = 2  # seconds between port-stat polls

# ── Active Inference hyperparameters ──────────────────────────────────────────
EFE_TEMPERATURE = 8.0
SWITCH_PROB_THRESHOLD = 0.52  # was 0.60 — lower so valid switches aren't blocked
REROUTE_MIN_IMPROVEMENT = 0.02  # was 0.05 — calibrated for measured load_delta
PREFERRED_UTIL = 0.2

# ── Multipath (SELECT group) ──────────────────────────────────────────────────
# When active path utilisation exceeds this threshold the controller switches
# from single-path EFE selection to a SELECT group that sprays traffic across
# ALL candidate paths weighted by their remaining headroom.
MULTIPATH_CONGESTION_THRESHOLD = 0.45  # fraction of link capacity

# ── Load estimation ───────────────────────────────────────────────────────────
FLOW_STATS_POLL_INTERVAL = 4  # seconds between OFPFlowStatsRequest polls
CONGESTION_INVALIDATE_THRESHOLD = 0.75  # util above which path cache is force-cleared

# ── ARP dedup ────────────────────────────────────────────────────────────────
ARP_CACHE_TTL = 2.0  # seconds

# ── Subnet filter ────────────────────────────────────────────────────────────
MININET_SUBNET = "10.0.0."  # only learn hosts in this /24

# ── State export ──────────────────────────────────────────────────────────────
STATE_JSON_PATH = "state.json"

# ── Blockchain layer (Section III / Fig. 2) ──────────────────────────────────
# Both flags default to True but must gate cleanly to False for regression
# testing: with both off, controller behaviour is unchanged from the
# pre-blockchain codebase.
ENABLE_MODEL_TRADING = True
ENABLE_AUDIT_LEDGER = True

# Controller id -> set of managed dpids (matches Nc ⊆ N from the paper).
# NOTE: this fixed {1: {1,2}, 2: {3,4}} shape is only the historical
# 4-switch default. Since the topology editor allows arbitrary switch
# counts/ids, the controller no longer trusts this constant at runtime —
# it recomputes the split dynamically from whatever dpids are actually
# discovered (see TopologyManager.compute_controller_domains() and
# ActiveInferenceDynamic._refresh_controller_domains()). Kept here only
# as documentation of the original scheme / for any code that wants a
# static reference value.
CONTROLLER_DOMAINS = {1: {1, 2}, 2: {3, 4}}

# Friendly DID labels per controller id (used by ControllerIdentity).
CONTROLLER_DID_LABELS = {1: "alpha", 2: "beta"}

# Simulated IPFS shard store tuning.
IPFS_NODE_COUNT = 3
MODEL_SHARD_COUNT = 4

# Throttle: publish own belief snapshot to the model store every N inference
# ticks (avoids hammering the chain on every cycle).
MODEL_PUBLISH_INTERVAL_TICKS = 5

# ── CIU (Congestion Index Unit) — Eq. 6 of the paper ────────────────────────
#   C = w_Y · Y_trs + w_G · G_pls + w_L · L_lld
# Y_trs is a util-derived delay proxy (Y_max = 100 ms means at util=1.0 the
# proxy equals 100 ms). G_pls is the per-link packet-loss fraction.
# L_lld is the per-link load (= utilisation). Each term is standardized
# against its threshold and clipped to [0, 1] before weighting.
CIU_WEIGHT_Y = 0.4  # transmission delay
CIU_WEIGHT_G = 0.3  # packet loss rate
CIU_WEIGHT_L = 0.3  # link load
CIU_Y_MAX_MS = 100.0
CIU_G_MAX = 0.1
CIU_L_MAX = 0.8
