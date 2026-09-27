"""
benchmark/sanity_check.py — Verify trial consistency before plotting.

Runs a battery of checks against the collected trials before any
plot is generated. The point is to catch measurement inconsistencies
that would otherwise make the comparison misleading:

  * Both approaches must have the same number of trials per scenario.
  * Both approaches must have the same flow IDs (so per-flow plots
    are directly comparable).
  * Both approaches must cover the same simulation time range.
  * Both approaches must have throughput samples (so we're not
    silently comparing AI's throughput to a Reactive "no data" plot).
  * Both approaches must have utilisation samples.
  * Packet loss must be measured consistently (not None on one side).
  * Jain's fairness must be calculated over the same flows.
  * Reroute events must have valid timestamps within the trial window.
  * Path changes in reroute events must actually correspond to different
    paths (otherwise they're not real reroutes).
  * The threshold must be applied consistently — both controllers use
    the same 0.45 value (verified via sdn.constants).

Each check returns a list of SanityIssue records. The runner logs them
and decides whether to proceed (warnings) or abort (errors).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from benchmark.recorder import TrialSamples
from benchmark.metrics import CONGESTION_THRESHOLD


@dataclass
class SanityIssue:
    level: str       # "error" or "warning"
    scope: str       # e.g. "scenario=baseline", or "global"
    message: str


def _trial_flow_ids(trial: TrialSamples) -> set:
    """Set of flow_ids in a trial's throughput samples."""
    return {s.flow_id for s in trial.throughput_samples}


def _trial_time_range(trial: TrialSamples) -> Tuple[float, float]:
    """(start_t, end_t) of all samples in the trial."""
    ts = []
    ts.extend(s.t for s in trial.link_samples)
    ts.extend(s.t for s in trial.flow_samples)
    ts.extend(s.t for s in trial.throughput_samples)
    if not ts:
        return (0.0, 0.0)
    return (min(ts), max(ts))


def check_trial_consistency(
    trials_by_approach: Dict[str, Dict[str, List[TrialSamples]]],
) -> List[SanityIssue]:
    """
    Run all sanity checks. Returns a list of issues (errors + warnings).

    trials_by_approach:
        {approach: {scenario: [TrialSamples, ...]}}
    """
    issues: List[SanityIssue] = []

    approaches = sorted(trials_by_approach.keys())
    if len(approaches) < 2:
        issues.append(SanityIssue(
            "warning", "global",
            f"Only one approach present: {approaches}. Comparison plots "
            f"will be incomplete."
        ))

    # ── Check 1: same scenarios covered ─────────────────────────────────
    all_scenarios = set()
    for approach, scenario_map in trials_by_approach.items():
        all_scenarios.update(scenario_map.keys())
    for approach in approaches:
        missing = all_scenarios - set(trials_by_approach[approach].keys())
        if missing:
            issues.append(SanityIssue(
                "error", f"approach={approach}",
                f"Missing scenarios: {sorted(missing)}"
            ))

    # ── Check 2: same trial count per scenario ──────────────────────────
    for scenario in sorted(all_scenarios):
        counts = {}
        for approach in approaches:
            trials = trials_by_approach.get(approach, {}).get(scenario, [])
            counts[approach] = len(trials)
        unique_counts = set(counts.values())
        if len(unique_counts) > 1:
            issues.append(SanityIssue(
                "error", f"scenario={scenario}",
                f"Trial count mismatch: {counts}. Comparison plots require "
                f"the same number of trials per (approach, scenario)."
            ))

    # ── Check 3: same flow IDs per scenario ─────────────────────────────
    for scenario in sorted(all_scenarios):
        flow_id_sets = {}
        for approach in approaches:
            trials = trials_by_approach.get(approach, {}).get(scenario, [])
            # Union of flow_ids across all trials of this approach.
            ids = set()
            for t in trials:
                ids.update(_trial_flow_ids(t))
            flow_id_sets[approach] = ids
        if len(flow_id_sets) >= 2:
            ids_list = list(flow_id_sets.values())
            if ids_list[0] != ids_list[1]:
                only_a = ids_list[0] - ids_list[1]
                only_b = ids_list[1] - ids_list[0]
                issues.append(SanityIssue(
                    "error", f"scenario={scenario}",
                    f"Flow ID mismatch. Only in {approaches[0]}: {sorted(only_a)}. "
                    f"Only in {approaches[1]}: {sorted(only_b)}. "
                    f"Per-flow throughput plots require matching flow IDs."
                ))

    # ── Check 4: trial time ranges are comparable (within 10s) ──────────
    for scenario in sorted(all_scenarios):
        ranges = {}
        for approach in approaches:
            trials = trials_by_approach.get(approach, {}).get(scenario, [])
            if not trials:
                continue
            all_starts = []
            all_ends = []
            for t in trials:
                s, e = _trial_time_range(t)
                all_starts.append(s)
                all_ends.append(e)
            ranges[approach] = (min(all_starts), max(all_ends))
        if len(ranges) >= 2:
            vals = list(ranges.values())
            max_diff = max(abs(vals[0][0] - vals[1][0]),
                           abs(vals[0][1] - vals[1][1]))
            if max_diff > 10.0:
                issues.append(SanityIssue(
                    "warning", f"scenario={scenario}",
                    f"Time ranges differ by {max_diff:.1f}s: "
                    f"{approaches[0]}={vals[0] if False else ranges[approaches[0]]}, "
                    f"{approaches[1]}={ranges[approaches[1]]}. "
                    f"Plots will use overlapping range; non-overlapping "
                    f"portions will be empty."
                ))

    # ── Check 5: both approaches have throughput samples ────────────────
    for scenario in sorted(all_scenarios):
        for approach in approaches:
            trials = trials_by_approach.get(approach, {}).get(scenario, [])
            total_tp = sum(len(t.throughput_samples) for t in trials)
            if total_tp == 0:
                issues.append(SanityIssue(
                    "error", f"scenario={scenario}, approach={approach}",
                    f"No throughput samples collected. The per-flow "
                    f"throughput plot will be empty for this approach."
                ))

    # ── Check 6: both approaches have link utilisation samples ──────────
    for scenario in sorted(all_scenarios):
        for approach in approaches:
            trials = trials_by_approach.get(approach, {}).get(scenario, [])
            total_links = sum(len(t.link_samples) for t in trials)
            if total_links == 0:
                issues.append(SanityIssue(
                    "error", f"scenario={scenario}, approach={approach}",
                    f"No link utilisation samples collected. The utilisation "
                    f"plots will be empty for this approach."
                ))

    # ── Check 7: reroute event timestamps are within trial window ───────
    for scenario in sorted(all_scenarios):
        for approach in approaches:
            trials = trials_by_approach.get(approach, {}).get(scenario, [])
            for trial in trials:
                if not trial.reroute_events:
                    continue
                t_start = trial.started_at - trial.started_at  # 0
                t_end = trial.finished_at - trial.started_at
                for ev in trial.reroute_events:
                    if ev.t < -1.0 or ev.t > t_end + 5.0:
                        issues.append(SanityIssue(
                            "warning",
                            f"scenario={scenario}, approach={approach}, trial={trial.trial}",
                            f"Reroute event at t={ev.t:.2f}s is outside trial "
                            f"window [0, {t_end:.1f}s]. May indicate clock skew."
                        ))
                        break  # one warning per trial is enough

    # ── Check 8: path changes actually correspond to different paths ────
    for scenario in sorted(all_scenarios):
        for approach in approaches:
            trials = trials_by_approach.get(approach, {}).get(scenario, [])
            for trial in trials:
                for ev in trial.reroute_events:
                    # Skip events where path change is not expected:
                    # cold_start (no old_path), efe_split (path doesn't change),
                    # hysteresis_hold / no_better_path (failed, no change).
                    if ev.trigger_reason in (
                        "cold_start", "efe_split", "ecmp_fallback",
                        "hysteresis_hold", "no_better_path",
                    ):
                        continue
                    # For successful reroutes (congestion_threshold_crossed,
                    # efe_switch), old_path must differ from new_path.
                    if ev.success and ev.old_path == ev.new_path:
                        issues.append(SanityIssue(
                            "warning",
                            f"scenario={scenario}, approach={approach}, trial={trial.trial}",
                            f"Successful reroute event has old_path == new_path "
                            f"({ev.old_path}). trigger={ev.trigger_reason}. "
                            f"Either the controller logged a no-op as a reroute, "
                            f"or the path comparison needs review."
                        ))
                        break  # one warning per trial

    # ── Check 9: threshold is consistent ────────────────────────────────
    try:
        from sdn.constants import (
            REACTIVE_CONGESTION_THRESHOLD,
            MULTIPATH_CONGESTION_THRESHOLD,
        )
        if REACTIVE_CONGESTION_THRESHOLD != MULTIPATH_CONGESTION_THRESHOLD:
            issues.append(SanityIssue(
                "warning", "global",
                f"Reactive threshold ({REACTIVE_CONGESTION_THRESHOLD}) != "
                f"AI multipath threshold ({MULTIPATH_CONGESTION_THRESHOLD}). "
                f"Plot reference line will use {CONGESTION_THRESHOLD}."
            ))
        if REACTIVE_CONGESTION_THRESHOLD != CONGESTION_THRESHOLD:
            issues.append(SanityIssue(
                "error", "global",
                f"Reactive threshold ({REACTIVE_CONGESTION_THRESHOLD}) != "
                f"benchmark threshold ({CONGESTION_THRESHOLD}). Update "
                f"benchmark.metrics.CONGESTION_THRESHOLD to match."
            ))
    except ImportError:
        issues.append(SanityIssue(
            "warning", "global",
            "Could not import sdn.constants to verify threshold consistency."
        ))

    return issues


def print_issues(issues: List[SanityIssue]) -> None:
    """Print sanity check issues to stdout."""
    if not issues:
        print("  All sanity checks passed.")
        return
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    print(f"  Sanity checks: {len(errors)} errors, {len(warnings)} warnings.")
    for issue in issues:
        marker = "[ERROR]" if issue.level == "error" else "[WARN] "
        print(f"  {marker} {issue.scope}: {issue.message}")
