"""
blockchain/audit.py — On-chain logging of per-cycle congestion metrics.

No Ryu imports. Pure Python + math.

This is the "smart contract continuously logs key network metrics on the
blockchain" behaviour from Section III.A: every routing decision (one per
flow per inference tick) appends a `metrics_log` block to the shared
ledger.

CIU (Congestion Index Unit) — Eq. 6 of the paper
─────────────────────────────────────────────────
    C = w_Y · Y_trs + w_G · G_pls + w_L · L_lld

where each term is standardized against a threshold and clipped to [0, 1]:
    Y_trs : transmission delay proxy    (Y_max   = 100 ms, derived from util)
    G_pls : packet-loss rate            (G_max   = 0.1)
    L_lld : link load                   (L_max   = 0.8)

Implementation note
-------------------
The existing codebase only derived `rate_mbps` / `util` from port byte
counters.  Packet-loss instrumentation was added in parallel to
`update_link_util` (see `sdn/topology_manager.py`):
  - port_stats_reply_handler now also tracks rx_dropped + tx_dropped
    deltas alongside rx_packets + tx_packets deltas.
  - TopologyManager.update_link_drops() stores a per-link loss_fraction
    in [0, 1] (drops / packets over the last interval).

`compute_ciu(load, loss_fraction)` takes the bottleneck load and loss
along the chosen path and returns a single CIU value in [0, 1].
"""

from __future__ import annotations

from typing import Optional

from blockchain.ledger import Ledger
from sdn.constants import (
    CIU_G_MAX,
    CIU_L_MAX,
    CIU_WEIGHT_G,
    CIU_WEIGHT_L,
    CIU_WEIGHT_Y,
    CIU_Y_MAX_MS,
)


def compute_ciu(load: float, loss_fraction: float) -> float:
    """
    Compute the Congestion Index Unit (Eq. 6).

    Parameters
    ----------
    load : float
        Bottleneck link load (utilisation) along the chosen path, in [0, 1].
    loss_fraction : float
        Bottleneck packet-loss fraction along the chosen path, in [0, 1].

    Returns
    -------
    float in [0, 1] — higher = more congested.
    """
    # Standardize each term against its threshold, then clip.
    # Y_trs is a util-derived delay proxy: at util=1.0 the proxy equals Y_max.
    y_trs_ms = load * CIU_Y_MAX_MS
    Y_std = min(max(y_trs_ms / CIU_Y_MAX_MS, 0.0), 1.0)

    G_std = min(max(loss_fraction / CIU_G_MAX, 0.0), 1.0)
    L_std = min(max(load / CIU_L_MAX, 0.0), 1.0)

    ciu = CIU_WEIGHT_Y * Y_std + CIU_WEIGHT_G * G_std + CIU_WEIGHT_L * L_std
    return min(max(ciu, 0.0), 1.0)


def log_cycle(
    ledger: Ledger,
    controller_id: str,
    src_ip: str,
    dst_ip: str,
    path: list,
    G: float,
    load_estimate: float,
    link_utils: dict,
    link_losses: Optional[dict] = None,
    ciu: Optional[float] = None,
    multipath_weights: Optional[list] = None,
) -> None:
    """
    Append one `metrics_log` block describing a single routing decision.

    Parameters
    ----------
    ledger           : the shared Ledger
    controller_id    : DID (or short id) of the controller making the decision
    src_ip, dst_ip   : flow endpoints
    path             : list of dpids chosen (active path)
    G                : expected free energy of the chosen path
    load_estimate    : EMA load estimate for the flow
    link_utils       : {(src_dpid, dst_dpid): util} for links along the path
    link_losses      : optional {(src_dpid, dst_dpid): loss_fraction}
    ciu              : optional precomputed CIU in [0, 1]. If None, the
                       block is still written but with ciu=None — callers
                       that cannot measure packet loss should pass None
                       rather than fabricate a value.
    multipath_weights: optional list of integer bucket weights (when
                       multipath SELECT group is active for this flow)
    """
    payload = {
        "controller_id": controller_id,
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "path": [f"s{d}" for d in path],
        "G": round(float(G), 5) if G is not None else None,
        "load_estimate": round(float(load_estimate), 4),
        "link_utils": {
            f"s{a}-s{b}": round(float(u), 4) for (a, b), u in link_utils.items()
        },
        "ciu": round(float(ciu), 4) if ciu is not None else None,
    }
    if link_losses is not None:
        payload["link_losses"] = {
            f"s{a}-s{b}": round(float(v), 6)
            for (a, b), v in link_losses.items()
        }
    if multipath_weights is not None:
        payload["multipath_weights"] = list(multipath_weights)

    ledger.append("metrics_log", payload)
