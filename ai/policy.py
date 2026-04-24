"""
ai/policy.py — Expected Free Energy computation and path selection policy.

No Ryu imports. Pure Python + math.
"""

import math

from ai.belief import PathBelief
from sdn.constants import (
    EFE_TEMPERATURE,
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
) -> float:
    """
    Compute Expected Free Energy G for routing flow to path_idx.

    G(path) = Σ_paths [ extrinsic_dev + congestion_penalty ]
            + epistemic_penalty(target_path)

    Two distinct actions are modelled:

    STAY  (path_idx == active_idx):
        The agent keeps the flow on its current path.
        → No load moves anywhere; every path retains its current utilisation.
        → Predicted util for ALL paths = belief.mu (no change).

        BUG that caused one-way switching:
        The old code always applied predict_after_load_removed() to active_idx
        even during STAY evaluation. This made STAY look artificially cheap
        (as if the load had vanished), so once path1 was active and congested,
        STAY still appeared better than SWITCH back to path0 — breaking
        symmetry and preventing the reverse switch.

    SWITCH (path_idx != active_idx):
        The agent moves the flow from active_idx to path_idx.
        → active_idx loses the h1<->h2 load  → predict_after_load_removed
        → path_idx gains the h1<->h2 load    → predict_after_load_added
        → every other path is unaffected      → belief.mu

    Extrinsic term: predicted deviation from preferred utilisation across all paths.
    Epistemic penalty: sigma_obs of the *target* path (prefer well-known paths).
    Congestion penalty: exponential above threshold.
    """
    # Determine whether this evaluation represents STAY or SWITCH.
    is_stay = path_idx == active_idx

    total_G = 0.0

    for idx, belief in beliefs.items():
        if is_stay:
            # ── STAY: no load movement; all paths keep current utilisation ──
            # Do NOT call predict_after_load_removed on active_idx here.
            # Doing so would incorrectly remove load from the current path,
            # making STAY appear cheaper than it truly is and preventing the
            # agent from ever switching back once it has left path0.
            predicted = belief.mu

        else:
            # ── SWITCH: load moves from active_idx to path_idx ──
            if idx == active_idx:
                # Active path sheds the flow load after switching away.
                predicted = belief.predict_after_load_removed(load_delta)
            elif idx == path_idx:
                # Target path absorbs the flow load after switching to it.
                predicted = belief.predict_after_load_added(load_delta)
            else:
                # All other paths are unaffected by this switching decision.
                predicted = belief.mu

        extrinsic = (predicted - preferred_util) ** 2 / (2 * sigma_prior**2)
        total_G += extrinsic

        if predicted > congestion_threshold:
            total_G += 5.0 * (predicted - congestion_threshold) ** 2

    # Epistemic penalty only for the target path (not the already-known active path)
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
    Compute EFE for all candidate paths and return the index of the best one.
    Uses softmax selection with hysteresis on the active path.
    """
    n = len(candidates)
    if n == 1:
        return 0

    G_values = {
        i: compute_efe_for_path(
            path_idx=i,
            active_idx=active_idx,
            beliefs=beliefs,
            load_delta=load_estimate,
            preferred_util=PREFERRED_UTIL,
        )
        for i in range(n)
    }

    # Numerically stable softmax
    g_min = min(G_values.values())
    exp_vals = {i: math.exp(-EFE_TEMPERATURE * (G_values[i] - g_min)) for i in G_values}
    Z = sum(exp_vals.values())
    probs = {i: exp_vals[i] / Z for i in exp_vals}

    best_idx = max(probs, key=lambda i: probs[i])

    # Hysteresis: only reroute if the improvement is meaningful
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
