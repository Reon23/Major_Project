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
