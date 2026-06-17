"""
ai/policy.py — Expected Free Energy computation and path selection policy.

No Ryu imports. Pure Python + math.
"""

import math

from ai.belief import PathBelief
from sdn.constants import (
    EFE_TEMPERATURE,
    MULTIPATH_CONGESTION_THRESHOLD,
    PREFERRED_UTIL,
    REROUTE_MIN_IMPROVEMENT,
    SWITCH_PROB_THRESHOLD,
)


def compute_efe_for_path(
    path_idx: int,
    active_idx: int,
    beliefs: dict,  # int -> PathBelief
    load_delta: float,
    preferred_util: float = PREFERRED_UTIL,
    sigma_prior: float = 0.15,
    congestion_threshold: float = 0.8,
    split_fraction: float = 1.0,
) -> float:
    """
    Compute Expected Free Energy G for routing flow to path_idx.

    split_fraction controls what fraction of load_delta moves:
      1.0  = full switch (original behaviour)
      0.0  = stay (original STAY behaviour)
      0..1 = partial load migration (load balancing)

    G(path) = Σ_paths [ extrinsic_dev + congestion_penalty ]
            + epistemic_penalty(target_path)

    STAY  (path_idx == active_idx):
        No load moves. All paths keep belief.mu.
        split_fraction is ignored.

    SWITCH / SPLIT (path_idx != active_idx):
        active_idx sheds  load_delta * split_fraction
        path_idx   absorbs load_delta * split_fraction
        All other paths are unaffected.
    """
    is_stay = path_idx == active_idx
    effective_delta = load_delta * split_fraction

    total_G = 0.0

    for idx, belief in beliefs.items():
        if is_stay:
            predicted = belief.mu
        else:
            if idx == active_idx:
                predicted = belief.predict_after_load_removed(effective_delta)
            elif idx == path_idx:
                predicted = belief.predict_after_load_added(effective_delta)
            else:
                predicted = belief.mu

        extrinsic = (predicted - preferred_util) ** 2 / (2 * sigma_prior**2)
        total_G += extrinsic

        if predicted > congestion_threshold:
            total_G += 5.0 * (predicted - congestion_threshold) ** 2

    target_belief = beliefs[path_idx]
    epistemic = target_belief.sigma_obs if path_idx != active_idx else 0.0
    total_G += epistemic

    return total_G


def select_best_path(
    flow_key: tuple,
    candidates: list,
    beliefs: dict,
    active_idx: int,
    load_estimate: float,
    logger=None,
) -> int:
    """
    Compute EFE for all candidate paths under both full-switch and split-load
    actions. Return the index of the path with the lowest expected free energy.
    Uses softmax selection with hysteresis on the active path.
    """
    n = len(candidates)
    if n == 1:
        return 0

    # Evaluate each candidate under two split fractions: full-switch and half
    SPLIT_FRACTIONS = [1.0, 0.5]

    G_values = {}
    for i in range(n):
        # For STAY, split_fraction is irrelevant; compute once
        if i == active_idx:
            G_values[i] = compute_efe_for_path(
                path_idx=i,
                active_idx=active_idx,
                beliefs=beliefs,
                load_delta=load_estimate,
                preferred_util=PREFERRED_UTIL,
                split_fraction=1.0,  # ignored for STAY
            )
        else:
            # Take the minimum G across split fractions (most attractive action)
            G_values[i] = min(
                compute_efe_for_path(
                    path_idx=i,
                    active_idx=active_idx,
                    beliefs=beliefs,
                    load_delta=load_estimate,
                    preferred_util=PREFERRED_UTIL,
                    split_fraction=sf,
                )
                for sf in SPLIT_FRACTIONS
            )

    # Numerically stable softmax
    g_min = min(G_values.values())
    exp_vals = {i: math.exp(-EFE_TEMPERATURE * (G_values[i] - g_min)) for i in G_values}
    Z = sum(exp_vals.values())
    probs = {i: exp_vals[i] / Z for i in exp_vals}

    best_idx = max(probs, key=lambda i: probs[i])

    # Hysteresis: only reroute if the improvement clears both thresholds
    if best_idx != active_idx:
        improvement = G_values[active_idx] - G_values[best_idx]
        p_reroute = probs[best_idx]
        if improvement < REROUTE_MIN_IMPROVEMENT or p_reroute < SWITCH_PROB_THRESHOLD:
            if logger:
                logger.info(
                    "Flow %s: hysteresis hold on path%d (improvement=%.4f, P=%.3f)",
                    flow_key,
                    active_idx,
                    improvement,
                    p_reroute,
                )
            best_idx = active_idx

    return best_idx


def compute_multipath_weights(
    candidates: list,
    beliefs: dict,
    load_estimate: float,
    preferred_util: float = PREFERRED_UTIL,
    congestion_threshold: float = MULTIPATH_CONGESTION_THRESHOLD,
) -> list:
    """
    Compute integer bucket weights for a SELECT group across all candidate paths.

    Strategy
    --------
    Weight each path by its available headroom:
        headroom_i = max(0, congestion_threshold - beliefs[i].mu)

    Paths already at or above the congestion threshold get weight 0 —
    the SELECT group will never send to a saturated port.

    Weights are normalised to integers in [0, 100] so OVS can use them
    directly as bucket weights.  If every path is congested, fall back
    to equal weights so traffic still flows.

    Returns
    -------
    list of int, one per candidate, same order as `candidates`.
    """
    n = len(candidates)
    if n == 1:
        return [1]

    headrooms = [max(0.0, congestion_threshold - beliefs[i].mu) for i in range(n)]

    total = sum(headrooms)
    if total <= 0.0:
        # All paths congested — equal weights (traffic must go somewhere)
        return [1] * n

    # Scale to integers 1..100 (minimum weight 1 so bucket is never dead)
    weights = [max(1, round(100 * h / total)) for h in headrooms]
    return weights
