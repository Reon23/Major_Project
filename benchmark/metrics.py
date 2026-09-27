"""
benchmark/metrics.py — Aggregate performance metrics for the comparison.

All metrics are computed from a list of TrialSamples (one per trial) and
reduced across trials (mean / std) to give per-(controller, scenario)
aggregates.

Honesty policy
--------------
The two controllers have different *design* biases:

  * Active Inference is anticipatory — it splits across paths *before*
    congestion bites, trading a small steady-state reroute count for
    smoother utilisation and lower loss under load.

  * Reactive is minimalist — it does nothing until forced to, so it
    will often have a *lower* reroute count at low load (it simply
    doesn't act), at the cost of higher loss / longer recovery time
    when congestion does hit.

Metrics that legitimately favour Active Inference under load:
  - mean / p95 link utilisation
  - mean packet loss
  - Jain's fairness across concurrent flows
  - time-to-recover from crossing the 0.45 threshold

Metrics that can legitimately favour Reactive at low load:
  - reroute / path-switch count

The compare/ summary output makes this tradeoff explicit.

NaN vs zero policy
------------------
Some metrics are genuinely *undefined* when a particular condition
didn't occur in a trial — e.g. "time to first reroute" is undefined
if there were no reroute events. We represent this with `None` (which
serialises to JSON `null`), NOT `0.0`. The aggregation step then
tracks how many trials had `None` for that metric so the report can
say "(2 of 3 trials had no reroutes — undefined)" rather than silently
treating that as "0s recovery".
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from benchmark.recorder import TrialSamples


CONGESTION_THRESHOLD = 0.45  # MULTIPATH_CONGESTION_THRESHOLD / REACTIVE_CONGESTION_THRESHOLD


# ── Per-trial scalar metrics ───────────────────────────────────────────────


def mean_link_util(trial: TrialSamples) -> float:
    """Mean link utilisation across all switch-switch links, all time."""
    if not trial.link_samples:
        return float("nan")
    return statistics.mean(s.util for s in trial.link_samples)


def max_link_util(trial: TrialSamples) -> float:
    """Maximum (bottleneck) link utilisation observed during the trial."""
    if not trial.link_samples:
        return float("nan")
    return max(s.util for s in trial.link_samples)


def p95_link_util(trial: TrialSamples) -> float:
    """95th percentile link utilisation across all links, all time."""
    if not trial.link_samples:
        return float("nan")
    utils = sorted(s.util for s in trial.link_samples)
    idx = max(0, int(math.ceil(0.95 * len(utils))) - 1)
    return utils[idx]


def mean_packet_loss(trial: TrialSamples) -> float:
    """Mean packet-loss fraction across all switch-switch links, all time."""
    if not trial.link_samples:
        return float("nan")
    return statistics.mean(s.loss_fraction for s in trial.link_samples)


def mean_throughput(trial: TrialSamples) -> float:
    """Mean throughput across all flow_stats events (Mbps)."""
    if not trial.throughput_samples:
        return float("nan")
    return statistics.mean(s.mbps for s in trial.throughput_samples)


def throughput_cov(trial: TrialSamples) -> float:
    """
    Coefficient of variation of throughput over time, averaged across flows.

    CoV = std / mean per flow, then averaged. Lower = more stable.
    Returns NaN if no throughput samples.
    """
    if not trial.throughput_samples:
        return float("nan")
    by_flow: Dict[str, List[float]] = {}
    for s in trial.throughput_samples:
        by_flow.setdefault(s.flow_id, []).append(s.mbps)
    covs: List[float] = []
    for flow_id, vals in by_flow.items():
        if len(vals) < 2:
            continue
        m = statistics.mean(vals)
        if m <= 0:
            continue
        sd = statistics.stdev(vals)
        covs.append(sd / m)
    if not covs:
        return float("nan")
    return statistics.mean(covs)


def jains_fairness(trial: TrialSamples) -> float:
    """
    Jain's fairness index across concurrent flows' mean throughput.

    Jain's index = (Σ xi)² / (n · Σ xi²), ∈ [1/n, 1]. 1 = perfectly fair.

    Returns NaN if no throughput samples.
    """
    if not trial.throughput_samples:
        return float("nan")
    by_flow: Dict[str, List[float]] = {}
    for s in trial.throughput_samples:
        by_flow.setdefault(s.flow_id, []).append(s.mbps)
    means = [statistics.mean(v) for v in by_flow.values() if v]
    if not means:
        return float("nan")
    n = len(means)
    sum_x = sum(means)
    sum_x2 = sum(x * x for x in means)
    if sum_x2 == 0:
        return float("nan")
    return (sum_x * sum_x) / (n * sum_x2)


# ── Reroute / congestion metrics ───────────────────────────────────────────


def total_reroute_count(trial: TrialSamples) -> int:
    """
    Total routing-decision events (including failed attempts and
    hysteresis holds) for this trial.

    This is the raw count of every event the controller logged — it
    captures how *active* the controller was, including failed
    reroute attempts.
    """
    return len(trial.reroute_events)


def successful_reroute_count(trial: TrialSamples) -> int:
    """
    Count of reroute events that actually changed the path (success=True
    AND old_path != new_path).

    Excludes: cold_start (no old_path), efe_split (path doesn't change),
    hysteresis_hold (success=False), no_better_path (success=False).
    """
    count = 0
    for e in trial.reroute_events:
        if not e.success:
            continue
        if e.trigger_reason in ("cold_start", "efe_split", "ecmp_fallback"):
            continue
        if e.old_path and e.new_path and e.old_path != e.new_path:
            count += 1
    return count


def failed_reroute_count(trial: TrialSamples) -> int:
    """
    Count of failed/ineffective reroute attempts (success=False).

    Includes hysteresis_hold (cooldown prevented reroute) and
    no_better_path (no alternative found).
    """
    return sum(1 for e in trial.reroute_events if not e.success)


def time_to_first_reroute(trial: TrialSamples) -> Optional[float]:
    """
    Seconds from trial start to the first successful path switch.

    Returns:
      None     if there were no successful reroutes
      >0.0     seconds to first reroute
    """
    for e in trial.reroute_events:
        if not e.success:
            continue
        if e.trigger_reason in ("cold_start", "efe_split", "ecmp_fallback"):
            continue
        if e.old_path and e.new_path and e.old_path != e.new_path:
            return max(0.0, e.t)
    return None


def congestion_events_count(trial: TrialSamples) -> int:
    """
    Number of distinct congestion events (threshold crossings) observed
    during the trial.

    A congestion event is the transition from below-threshold to
    at/above-threshold on any link. Counted per-link, then summed —
    a flow crossing the threshold twice counts as 2 events.
    """
    if not trial.link_samples:
        return 0
    # Group samples per link, sorted by t.
    by_link: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}
    for s in trial.link_samples:
        by_link.setdefault((s.src, s.dst), []).append((s.t, s.util))
    for k in by_link:
        by_link[k].sort(key=lambda x: x[0])

    count = 0
    for samples in by_link.values():
        was_over = False
        for _t, u in samples:
            is_over = u >= CONGESTION_THRESHOLD
            if is_over and not was_over:
                count += 1
            was_over = is_over
    return count


def time_to_recover(trial: TrialSamples) -> Optional[float]:
    """
    Seconds from the first link crossing the 0.45 threshold to the
    first subsequent sample where the same link drops back under it.

    Returns:
      None     if no link ever crossed the threshold ("never crossed")
      0.0      if a link was already over at the first sample
      >0.0     seconds to recover

    The trial-level metric is the maximum over all links that crossed
    — the worst-case recovery.
    """
    if not trial.link_samples:
        return None

    by_link: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}
    for s in trial.link_samples:
        by_link.setdefault((s.src, s.dst), []).append((s.t, s.util))
    for k in by_link:
        by_link[k].sort(key=lambda x: x[0])

    worst: Optional[float] = None
    crossed_any = False
    for samples in by_link.values():
        first_over_idx: Optional[int] = None
        for i, (_t, u) in enumerate(samples):
            if u >= CONGESTION_THRESHOLD:
                first_over_idx = i
                crossed_any = True
                break
        if first_over_idx is None:
            continue
        first_over_t = samples[first_over_idx][0]
        recovered_t: Optional[float] = None
        for j in range(first_over_idx + 1, len(samples)):
            if samples[j][1] < CONGESTION_THRESHOLD:
                recovered_t = samples[j][0]
                break
        if recovered_t is None:
            recovered_t = samples[-1][0]
        recover_seconds = recovered_t - first_over_t
        if worst is None or recover_seconds > worst:
            worst = recover_seconds

    if not crossed_any:
        return None
    return worst if worst is not None else 0.0


# ── Aggregate per (controller, scenario) ────────────────────────────────────


@dataclass
class MetricStats:
    """mean / std / min / max across trials for one metric.

    `n_none` tracks how many trials had None (undefined) for this metric.
    """

    mean: float
    std: float
    min_val: float
    max_val: float
    n_trials: int
    n_none: int = 0

    def to_dict(self) -> Dict:
        # Use None for NaN — JSON null. Distinguish "measured zero" (0.0)
        # from "undefined" (None).
        if math.isnan(self.mean):
            return {
                "mean": None,
                "std": None,
                "min": None,
                "max": None,
                "n_trials": self.n_trials,
                "n_none": self.n_none,
            }
        return {
            "mean": round(self.mean, 5),
            "std": round(self.std, 5),
            "min": round(self.min_val, 5),
            "max": round(self.max_val, 5),
            "n_trials": self.n_trials,
            "n_none": self.n_none,
        }


@dataclass
class ControllerScenarioMetrics:
    """All aggregated metrics for one (controller, scenario) pair."""

    controller: str
    scenario: str
    n_trials: int

    mean_link_util: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    max_link_util: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    p95_link_util: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    mean_packet_loss: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    mean_throughput: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    throughput_cov: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    jains_fairness: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    reroute_count: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    successful_reroutes: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    failed_reroutes: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    time_to_first_reroute: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    time_to_recover: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))
    congestion_events: MetricStats = field(default_factory=lambda: MetricStats(float("nan"), 0, 0, 0, 0))

    def to_dict(self) -> Dict:
        return {
            "controller": self.controller,
            "scenario": self.scenario,
            "n_trials": self.n_trials,
            "mean_link_util": self.mean_link_util.to_dict(),
            "max_link_util": self.max_link_util.to_dict(),
            "p95_link_util": self.p95_link_util.to_dict(),
            "mean_packet_loss": self.mean_packet_loss.to_dict(),
            "mean_throughput": self.mean_throughput.to_dict(),
            "throughput_cov": self.throughput_cov.to_dict(),
            "jains_fairness": self.jains_fairness.to_dict(),
            "reroute_count": self.reroute_count.to_dict(),
            "successful_reroutes": self.successful_reroutes.to_dict(),
            "failed_reroutes": self.failed_reroutes.to_dict(),
            "time_to_first_reroute": self.time_to_first_reroute.to_dict(),
            "time_to_recover": self.time_to_recover.to_dict(),
            "congestion_events": self.congestion_events.to_dict(),
        }


def _stats_from_values(
    values: List[float], n_none: int = 0
) -> MetricStats:
    """Compute mean/std/min/max for a list of trial-level values.

    NaN values (from trials where the metric was undefined) are tracked
    in n_none and excluded from mean/std/min/max. If ALL values are NaN,
    the stats are NaN too.
    """
    n = len(values)
    if n == 0:
        return MetricStats(float("nan"), 0.0, 0.0, 0.0, 0, n_none)
    # Filter out NaN.
    finite = [v for v in values if not math.isnan(v)]
    if not finite:
        # All trials had undefined values for this metric.
        return MetricStats(float("nan"), 0.0, 0.0, 0.0, n, n)
    mean = statistics.mean(finite)
    sd = statistics.stdev(finite) if len(finite) >= 2 else 0.0
    return MetricStats(
        mean=mean,
        std=sd,
        min_val=min(finite),
        max_val=max(finite),
        n_trials=n,
        n_none=n_none,
    )


def _stats_from_optional_values(
    values: List[Optional[float]],
) -> MetricStats:
    """Compute stats for a list that may contain None values.

    None values are tracked in n_none and excluded from the statistics.
    If ALL values are None, the stats are NaN.
    """
    n = len(values)
    if n == 0:
        return MetricStats(float("nan"), 0.0, 0.0, 0.0, 0, 0)
    finite = [v for v in values if v is not None]
    n_none = n - len(finite)
    if not finite:
        return MetricStats(float("nan"), 0.0, 0.0, 0.0, n, n_none)
    mean = statistics.mean(finite)
    sd = statistics.stdev(finite) if len(finite) >= 2 else 0.0
    return MetricStats(
        mean=mean,
        std=sd,
        min_val=min(finite),
        max_val=max(finite),
        n_trials=n,
        n_none=n_none,
    )


def aggregate_metrics(
    trials: List[TrialSamples],
) -> ControllerScenarioMetrics:
    """Compute aggregated metrics across all trials for one
    (controller, scenario) pair."""
    if not trials:
        raise ValueError("aggregate_metrics: no trials provided")
    controller = trials[0].controller
    scenario = trials[0].scenario

    means_util = [mean_link_util(t) for t in trials]
    maxs_util = [max_link_util(t) for t in trials]
    p95s_util = [p95_link_util(t) for t in trials]
    losses = [mean_packet_loss(t) for t in trials]
    throughputs = [mean_throughput(t) for t in trials]
    covs = [throughput_cov(t) for t in trials]
    jains = [jains_fairness(t) for t in trials]
    reroutes = [float(total_reroute_count(t)) for t in trials]
    succ_reroutes = [float(successful_reroute_count(t)) for t in trials]
    fail_reroutes = [float(failed_reroute_count(t)) for t in trials]
    ttr_raw = [time_to_recover(t) for t in trials]
    ttfr_raw = [time_to_first_reroute(t) for t in trials]
    ce_raw = [float(congestion_events_count(t)) for t in trials]

    return ControllerScenarioMetrics(
        controller=controller,
        scenario=scenario,
        n_trials=len(trials),
        mean_link_util=_stats_from_values(means_util),
        max_link_util=_stats_from_values(maxs_util),
        p95_link_util=_stats_from_values(p95s_util),
        mean_packet_loss=_stats_from_values(losses),
        mean_throughput=_stats_from_values(throughputs),
        throughput_cov=_stats_from_values(covs),
        jains_fairness=_stats_from_values(jains),
        reroute_count=_stats_from_values(reroutes),
        successful_reroutes=_stats_from_values(succ_reroutes),
        failed_reroutes=_stats_from_values(fail_reroutes),
        time_to_first_reroute=_stats_from_optional_values(ttfr_raw),
        time_to_recover=_stats_from_optional_values(ttr_raw),
        congestion_events=_stats_from_values(ce_raw),
    )


# ── Honesty helpers: which side does each metric favour? ────────────────────


METRIC_DIRECTION = {
    "mean_link_util":         "lower",
    "max_link_util":          "lower",
    "p95_link_util":          "lower",
    "mean_packet_loss":       "lower",
    "mean_throughput":        "higher",
    "throughput_cov":         "lower",
    "jains_fairness":         "higher",
    "reroute_count":          "contextual",
    "successful_reroutes":    "contextual",
    "failed_reroutes":        "lower",
    "time_to_first_reroute":  "contextual",  # sooner = more reactive, but maybe premature
    "time_to_recover":        "lower",
    "congestion_events":      "lower",  # fewer threshold crossings = smoother
}


METRIC_FAVORS_AT_LOAD = {
    "mean_link_util":         {"low_load": "reactive",      "high_load": "active_inference"},
    "max_link_util":          {"low_load": "tie",           "high_load": "active_inference"},
    "p95_link_util":          {"low_load": "tie",           "high_load": "active_inference"},
    "mean_packet_loss":       {"low_load": "tie",           "high_load": "active_inference"},
    "mean_throughput":        {"low_load": "tie",           "high_load": "active_inference"},
    "throughput_cov":         {"low_load": "tie",           "high_load": "active_inference"},
    "jains_fairness":         {"low_load": "tie",           "high_load": "active_inference"},
    "reroute_count":          {"low_load": "reactive",      "high_load": "active_inference"},
    "successful_reroutes":    {"low_load": "reactive",      "high_load": "active_inference"},
    "failed_reroutes":        {"low_load": "tie",           "high_load": "active_inference"},
    "time_to_first_reroute":  {"low_load": "tie",           "high_load": "active_inference"},
    "time_to_recover":        {"low_load": "tie",           "high_load": "active_inference"},
    "congestion_events":      {"low_load": "tie",           "high_load": "active_inference"},
}


METRIC_LABELS = {
    "mean_link_util":         "Mean link util",
    "max_link_util":          "Max link util (bottleneck)",
    "p95_link_util":          "P95 link util",
    "mean_packet_loss":       "Mean packet loss",
    "mean_throughput":        "Mean throughput (Mbps)",
    "throughput_cov":         "Throughput CoV (lower=better)",
    "jains_fairness":         "Jain's fairness (higher=better)",
    "reroute_count":          "Total reroute events",
    "successful_reroutes":    "Successful reroutes",
    "failed_reroutes":        "Failed reroute attempts",
    "time_to_first_reroute":  "Time to first reroute (s)",
    "time_to_recover":        "Time to recover (s)",
    "congestion_events":      "Congestion events count",
}


def classify_scenario_load(scenario_name: str) -> str:
    """Classify a scenario as 'low_load' or 'high_load'."""
    if scenario_name in ("forced_bottleneck", "load_ramp"):
        return "high_load"
    return "low_load"
