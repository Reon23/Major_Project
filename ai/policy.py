"""
ai/policy.py — Expected Free Energy computation and path selection policy.

No Ryu imports. Pure Python + math.
"""

import math

from ai.belief import PathBelief
from sdn.constants import (
    EFE_TEMPERATURE,
    FULL_COMMIT_MARGIN,
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


def decide_routing_action(
    flow_key: tuple,
    candidates: list,
    beliefs: dict,
    active_idx: int,
    load_estimate: float,
    logger=None,
) -> tuple:
    """
    Three-way Active Inference routing decision: stay / switch / split.

    Background — why this exists
    -----------------------------
    `select_best_path` (above) evaluates each alternative path under two
    split fractions (1.0 = full switch, 0.5 = half-split) and takes
    whichever framing gives the lower G, but the caller then always
    *fully commits* to one path — there was never an actual action
    corresponding to "half-split". At low/medium load this mismatch shows
    up as visible oscillation: whenever the model's genuine preference is
    a **partial** load migration (the 0.5-split G beats the 1.0-switch
    G), forcing a binary stay/switch choice leaves the two options nearly
    tied, and small measurement noise flips the pick tick to tick — the
    controller "just switches between paths" instead of distributing
    load, even though multiple decent paths exist.

    This function keeps the same hysteresis-gated "is acting worth it at
    all" check as `select_best_path`, but — once acting is warranted —
    separately compares a genuine full-commit action against a genuine
    half-split action. A real tie between them now resolves to SPLIT
    (the caller installs a real multipath SELECT group across all
    candidates) instead of an arbitrary full commit that has nothing
    stable to converge to. A full commit is only chosen when it clearly
    beats splitting by FULL_COMMIT_MARGIN.

    Returns
    -------
    (action, target_idx) — action is one of "stay", "switch", "split".
    target_idx is the path to fully commit to for "switch"; for "stay"
    and "split" it's the best alternative index, informational only (the
    caller installs across every candidate for "split", not just this
    one).
    """
    n = len(candidates)
    if n == 1:
        return "stay", active_idx

    g_stay = compute_efe_for_path(
        path_idx=active_idx,
        active_idx=active_idx,
        beliefs=beliefs,
        load_delta=load_estimate,
        split_fraction=1.0,  # ignored for STAY
    )

    g_full, g_half = {}, {}
    for i in range(n):
        if i == active_idx:
            continue
        g_full[i] = compute_efe_for_path(
            path_idx=i,
            active_idx=active_idx,
            beliefs=beliefs,
            load_delta=load_estimate,
            split_fraction=1.0,
        )
        g_half[i] = compute_efe_for_path(
            path_idx=i,
            active_idx=active_idx,
            beliefs=beliefs,
            load_delta=load_estimate,
            split_fraction=0.5,
        )

    best_full_idx = min(g_full, key=g_full.get)
    best_half_idx = min(g_half, key=g_half.get)
    best_full_g = g_full[best_full_idx]
    best_half_g = g_half[best_half_idx]

    # Softmax over {stay, best-full, best-split} — same temperature-scaled
    # probability read as select_best_path, just over three options
    # instead of two, so P(act) still has to clear SWITCH_PROB_THRESHOLD.
    g_min = min(g_stay, best_full_g, best_half_g)
    exp_vals = {
        "stay": math.exp(-EFE_TEMPERATURE * (g_stay - g_min)),
        "switch": math.exp(-EFE_TEMPERATURE * (best_full_g - g_min)),
        "split": math.exp(-EFE_TEMPERATURE * (best_half_g - g_min)),
    }
    Z = sum(exp_vals.values())
    probs = {k: v / Z for k, v in exp_vals.items()}

    best_act_g = min(best_full_g, best_half_g)
    improvement = g_stay - best_act_g
    p_act = probs["switch"] + probs["split"]

    if improvement < REROUTE_MIN_IMPROVEMENT or p_act < SWITCH_PROB_THRESHOLD:
        if logger:
            logger.info(
                "Flow %s: hysteresis hold on path%d (improvement=%.4f, P_act=%.3f)",
                flow_key,
                active_idx,
                improvement,
                p_act,
            )
        return "stay", active_idx

    # Acting clears hysteresis — decide full commit vs. split. Ties (the
    # common case at low/medium load) resolve to split on purpose.
    if best_full_g + FULL_COMMIT_MARGIN < best_half_g:
        return "switch", best_full_idx

    return "split", best_half_idx


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
