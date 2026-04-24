"""
sdn/constants.py — All project-wide constants.
"""

# ── Ethertypes ───────────────────────────────────────────────────────────────
ETH_TYPE_IP   = 0x0800
ETH_TYPE_ARP  = 0x0806
ETH_TYPE_LLDP = 0x88CC

# ── ARP opcodes ──────────────────────────────────────────────────────────────
ARP_REQUEST = 1
ARP_REPLY   = 2

# ── OpenFlow priorities ──────────────────────────────────────────────────────
PRIORITY_TABLE_MISS = 0
PRIORITY_FLOW       = 20     # forwarding rules
PRIORITY_ARP_IN     = 10     # not currently installed as a rule; kept for reference

# ── Flow timeouts ─────────────────────────────────────────────────────────────
FLOW_IDLE_TIMEOUT = 30       # seconds; 0 = permanent
FLOW_HARD_TIMEOUT = 0        # no hard timeout

# ── Topology / bandwidth ──────────────────────────────────────────────────────
DEFAULT_LINK_BW_MBPS  = 10.0
MAX_CANDIDATE_PATHS   = 4

# ── Monitoring ────────────────────────────────────────────────────────────────
POLL_INTERVAL         = 2    # seconds between port-stat polls

# ── Active Inference hyperparameters ─────────────────────────────────────────
EFE_TEMPERATURE       = 8.0
SWITCH_PROB_THRESHOLD = 0.60
REROUTE_MIN_IMPROVEMENT = 0.05
PREFERRED_UTIL        = 0.2

# ── ARP dedup ────────────────────────────────────────────────────────────────
ARP_CACHE_TTL = 2.0          # seconds

# ── Subnet filter ────────────────────────────────────────────────────────────
MININET_SUBNET        = "10.0.0."   # only learn hosts in this /24

# ── State export ──────────────────────────────────────────────────────────────
STATE_JSON_PATH       = "state.json"
