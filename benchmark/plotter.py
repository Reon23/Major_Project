"""
benchmark/plotter.py — matplotlib comparison plots + summary table.

Generates the 10 plot types requested (A through J), plus a summary
markdown report with the honest tradeoff analysis.

All plots use the same axes/scales for Active Inference and Reactive so
the comparison is visually honest. Empty data (e.g. Reactive with zero
reroute events) is shown as an explicit "no events" placeholder, not
silently omitted.

Output structure
----------------
plots/
├── A_reroute_events_over_time_<scenario>.png   (per-scenario)
├── B_max_link_util_<scenario>.png              (per-scenario)
├── C_mean_link_util_<scenario>.png             (per-scenario)
├── D_p95_util_<scenario>.png                   (per-scenario)
├── E_packet_loss_<scenario>.png                (per-scenario)
├── F_aggregate_throughput_<scenario>.png       (per-scenario)
├── G_per_flow_throughput_<scenario>.png        (per-scenario)
├── H_jains_fairness.png                       (cross-scenario bar)
├── I_reroute_count.png                        (cross-scenario bar)
├── J_time_to_recover.png                      (cross-scenario bar)
├── summary.md
└── summary.json
"""

from __future__ import annotations

import logging
import os
import statistics
from typing import Any, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")

import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

# Per rule 7 of the system prompt: register Noto Sans SC + DejaVu Sans.
for _font_path in (
    "/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
):
    try:
        fm.fontManager.addfont(_font_path)
    except Exception:
        pass
plt.rcParams["font.sans-serif"] = ["Noto Sans SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

from benchmark.metrics import (  # noqa: E402
    CONGESTION_THRESHOLD,
    ControllerScenarioMetrics,
    aggregate_metrics,
    classify_scenario_load,
    jains_fairness,
    mean_link_util,
    max_link_util,
    mean_packet_loss,
    mean_throughput,
    p95_link_util,
    total_reroute_count,
    successful_reroute_count,
    failed_reroute_count,
    time_to_first_reroute,
    throughput_cov,
    time_to_recover,
    congestion_events_count,
    METRIC_DIRECTION,
    METRIC_FAVORS_AT_LOAD,
    METRIC_LABELS,
)
from benchmark.recorder import TrialSamples  # noqa: E402

_log = logging.getLogger(__name__)


COLOR_AI = "#58a6ff"
COLOR_REACTIVE = "#f0883e"
COLOR_THRESHOLD = "#f85149"
COLOR_FILL = "#21262d"


# ── Time-series aggregation helpers ───────────────────────────────────────


def _max_link_util_over_time(
    trials: List[TrialSamples],
) -> Tuple[List[float], List[float], List[float]]:
    """Per-timestep max link utilisation, averaged across trials.

    Returns (t_values, mean_values, std_values).
    """
    if not trials:
        return [], [], []
    bucket = 2.0  # POLL_INTERVAL
    by_bucket: Dict[float, List[float]] = {}
    for trial in trials:
        per_t: Dict[float, List[float]] = {}
        for s in trial.link_samples:
            t_b = round(s.t / bucket) * bucket
            per_t.setdefault(t_b, []).append(s.util)
        for t_b, utils in per_t.items():
            by_bucket.setdefault(t_b, []).append(max(utils))
    if not by_bucket:
        return [], [], []
    t_sorted = sorted(by_bucket.keys())
    means = [statistics.mean(by_bucket[t]) for t in t_sorted]
    stds = [statistics.stdev(by_bucket[t]) if len(by_bucket[t]) >= 2 else 0.0
            for t in t_sorted]
    return t_sorted, means, stds


def _mean_link_util_over_time(
    trials: List[TrialSamples],
) -> Tuple[List[float], List[float], List[float]]:
    """Per-timestep mean link utilisation across all links, averaged across trials."""
    if not trials:
        return [], [], []
    bucket = 2.0
    by_bucket: Dict[float, List[float]] = {}
    for trial in trials:
        per_t: Dict[float, List[float]] = {}
        for s in trial.link_samples:
            t_b = round(s.t / bucket) * bucket
            per_t.setdefault(t_b, []).append(s.util)
        for t_b, utils in per_t.items():
            by_bucket.setdefault(t_b, []).append(statistics.mean(utils))
    if not by_bucket:
        return [], [], []
    t_sorted = sorted(by_bucket.keys())
    means = [statistics.mean(by_bucket[t]) for t in t_sorted]
    stds = [statistics.stdev(by_bucket[t]) if len(by_bucket[t]) >= 2 else 0.0
            for t in t_sorted]
    return t_sorted, means, stds


def _packet_loss_over_time(
    trials: List[TrialSamples],
) -> Tuple[List[float], List[float], List[float]]:
    """Per-timestep mean packet-loss fraction, averaged across trials."""
    if not trials:
        return [], [], []
    bucket = 2.0
    by_bucket: Dict[float, List[float]] = {}
    for trial in trials:
        per_t: Dict[float, List[float]] = {}
        for s in trial.link_samples:
            t_b = round(s.t / bucket) * bucket
            per_t.setdefault(t_b, []).append(s.loss_fraction)
        for t_b, losses in per_t.items():
            by_bucket.setdefault(t_b, []).append(statistics.mean(losses))
    if not by_bucket:
        return [], [], []
    t_sorted = sorted(by_bucket.keys())
    means = [statistics.mean(by_bucket[t]) for t in t_sorted]
    stds = [statistics.stdev(by_bucket[t]) if len(by_bucket[t]) >= 2 else 0.0
            for t in t_sorted]
    return t_sorted, means, stds


def _aggregate_throughput_over_time(
    trials: List[TrialSamples],
) -> Tuple[List[float], List[float], List[float]]:
    """Per-timestep total throughput (sum of all flows), averaged across trials."""
    if not trials:
        return [], [], []
    bucket = 1.0  # iperf3 reports at 1s intervals
    by_bucket: Dict[float, List[float]] = {}
    for trial in trials:
        per_t: Dict[float, List[float]] = {}
        for s in trial.throughput_samples:
            t_b = round(s.t / bucket) * bucket
            per_t.setdefault(t_b, []).append(s.mbps)
        for t_b, mbps_vals in per_t.items():
            by_bucket.setdefault(t_b, []).append(sum(mbps_vals))
    if not by_bucket:
        return [], [], []
    t_sorted = sorted(by_bucket.keys())
    means = [statistics.mean(by_bucket[t]) for t in t_sorted]
    stds = [statistics.stdev(by_bucket[t]) if len(by_bucket[t]) >= 2 else 0.0
            for t in t_sorted]
    return t_sorted, means, stds


def _per_flow_throughput_over_time(
    trials: List[TrialSamples],
) -> Dict[str, Tuple[List[float], List[float], List[float]]]:
    """For each flow_id, return (t_values, mean_throughput, std_throughput)."""
    if not trials:
        return {}
    bucket = 1.0
    by_flow: Dict[str, Dict[float, List[float]]] = {}
    for trial in trials:
        for s in trial.throughput_samples:
            t_b = round(s.t / bucket) * bucket
            by_flow.setdefault(s.flow_id, {}).setdefault(t_b, []).append(s.mbps)
    out: Dict[str, Tuple[List[float], List[float], List[float]]] = {}
    for flow_id, by_t in by_flow.items():
        t_sorted = sorted(by_t.keys())
        means = [statistics.mean(by_t[t]) for t in t_sorted]
        stds = [statistics.stdev(by_t[t]) if len(by_t[t]) >= 2 else 0.0
                for t in t_sorted]
        out[flow_id] = (t_sorted, means, stds)
    return out


def _reroute_events_aggregated(
    trials: List[TrialSamples],
) -> List[Tuple[float, str, str]]:
    """Aggregate reroute events across trials: list of (t, flow_id, reason)."""
    out: List[Tuple[float, str, str]] = []
    for trial in trials:
        for ev in trial.reroute_events:
            # Skip cold_start — it's not a "decision" in the same sense.
            if ev.trigger_reason == "cold_start":
                continue
            out.append((ev.t, ev.flow_id, ev.trigger_reason))
    return out


# ── Plot A: Reroute events over time ──────────────────────────────────────


def plot_A_reroute_events(
    scenario_name: str,
    ai_trials: List[TrialSamples],
    reactive_trials: List[TrialSamples],
    out_path: str,
) -> None:
    """Scatter of reroute events over time, per controller."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 4), constrained_layout=True)
    fig.suptitle(
        f"A. Scenario: {scenario_name} — reroute events over time",
        fontsize=13,
    )
    for ax, trials, color, label in (
        (axes[0], ai_trials, COLOR_AI, "Active Inference"),
        (axes[1], reactive_trials, COLOR_REACTIVE, "Reactive"),
    ):
        events = _reroute_events_aggregated(trials)
        if not events:
            ax.text(0.5, 0.5, "no reroute events\n(this is a real result)",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#8b949e", fontsize=12)
        else:
            ts = [e[0] for e in events]
            ys = list(range(len(events)))
            # Color-code by trigger_reason.
            reason_colors = {
                "congestion_threshold_crossed": "#f0883e",
                "no_better_path": "#f85149",
                "ecmp_fallback": "#a371f7",
                "hysteresis_hold": "#d2a8ff",
                "efe_switch": "#58a6ff",
                "efe_split": "#79c0ff",
            }
            for i, (t, fid, reason) in enumerate(events):
                ax.scatter(t, i, color=reason_colors.get(reason, color),
                           s=60, alpha=0.85, zorder=3, edgecolors="white",
                           linewidth=0.5)
                if i < 12:  # label first 12 events
                    ax.annotate(fid, (t, i), fontsize=7, xytext=(4, 4),
                                textcoords="offset points", color="#8b949e")
            # Legend for trigger_reason colors.
            from matplotlib.patches import Patch
            seen_reasons = sorted({e[2] for e in events})
            handles = [
                Patch(facecolor=reason_colors.get(r, color), label=r)
                for r in seen_reasons
            ]
            ax.legend(handles=handles, loc="upper right", fontsize=8)
        ax.set_title(label)
        ax.set_xlabel("time (s)")
        ax.set_ylabel("event #")
        ax.grid(True, alpha=0.3)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot B: Max link utilisation over time ─────────────────────────────────


def plot_B_max_link_util(
    scenario_name: str,
    ai_trials: List[TrialSamples],
    reactive_trials: List[TrialSamples],
    out_path: str,
) -> None:
    """Max (bottleneck) link utilisation over time, with 0.45 threshold."""
    fig, ax = plt.subplots(1, 1, figsize=(10, 5), constrained_layout=True)
    fig.suptitle(
        f"B. Scenario: {scenario_name} — max link utilisation over time",
        fontsize=13,
    )
    _plot_timeseries(ax, ai_trials, COLOR_AI, "Active Inference",
                     _max_link_util_over_time)
    _plot_timeseries(ax, reactive_trials, COLOR_REACTIVE, "Reactive",
                     _max_link_util_over_time)
    ax.axhline(y=CONGESTION_THRESHOLD, color=COLOR_THRESHOLD,
               linestyle="--", linewidth=1.5,
               label=f"threshold = {CONGESTION_THRESHOLD}", alpha=0.8)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("max link utilisation [0..1]")
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot C: Mean link utilisation over time ────────────────────────────────


def plot_C_mean_link_util(
    scenario_name: str,
    ai_trials: List[TrialSamples],
    reactive_trials: List[TrialSamples],
    out_path: str,
) -> None:
    """Mean link utilisation over time."""
    fig, ax = plt.subplots(1, 1, figsize=(10, 5), constrained_layout=True)
    fig.suptitle(
        f"C. Scenario: {scenario_name} — mean link utilisation over time",
        fontsize=13,
    )
    _plot_timeseries(ax, ai_trials, COLOR_AI, "Active Inference",
                     _mean_link_util_over_time)
    _plot_timeseries(ax, reactive_trials, COLOR_REACTIVE, "Reactive",
                     _mean_link_util_over_time)
    ax.axhline(y=CONGESTION_THRESHOLD, color=COLOR_THRESHOLD,
               linestyle="--", linewidth=1.5,
               label=f"threshold = {CONGESTION_THRESHOLD}", alpha=0.8)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("mean link utilisation [0..1]")
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot D: P95 utilisation (bar chart of per-trial p95 values) ────────────


def plot_D_p95_util(
    scenario_name: str,
    ai_trials: List[TrialSamples],
    reactive_trials: List[TrialSamples],
    out_path: str,
) -> None:
    """Per-trial p95 link utilisation, scatter + mean line."""
    fig, ax = plt.subplots(1, 1, figsize=(8, 5), constrained_layout=True)
    fig.suptitle(
        f"D. Scenario: {scenario_name} — P95 link utilisation per trial",
        fontsize=13,
    )
    ai_vals = [p95_link_util(t) for t in ai_trials]
    r_vals = [p95_link_util(t) for t in reactive_trials]
    for i, (label, vals, color) in enumerate([
        ("Active Inference", ai_vals, COLOR_AI),
        ("Reactive", r_vals, COLOR_REACTIVE),
    ]):
        if not vals:
            continue
        x = [i + 1] * len(vals)
        ax.scatter(x, vals, color=color, s=80, alpha=0.7, zorder=3,
                   edgecolors="white", linewidth=0.5)
        m = statistics.mean(vals)
        ax.hlines(m, i + 0.7, i + 1.3, colors=color, linewidth=3, zorder=4,
                  label=f"{label} mean={m:.3f}")
    ax.axhline(y=CONGESTION_THRESHOLD, color=COLOR_THRESHOLD,
               linestyle="--", linewidth=1.2, alpha=0.7,
               label=f"threshold = {CONGESTION_THRESHOLD}")
    ax.set_xticks([1, 2])
    ax.set_xticklabels(["Active Inference", "Reactive"])
    ax.set_ylabel("P95 link utilisation")
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper right", fontsize=9)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot E: Packet loss (bar chart of per-trial mean loss) ────────────────


def plot_E_packet_loss(
    scenario_name: str,
    ai_trials: List[TrialSamples],
    reactive_trials: List[TrialSamples],
    out_path: str,
) -> None:
    """Per-trial mean packet loss, scatter + mean line."""
    fig, ax = plt.subplots(1, 1, figsize=(8, 5), constrained_layout=True)
    fig.suptitle(
        f"E. Scenario: {scenario_name} — mean packet loss per trial",
        fontsize=13,
    )
    ai_vals = [mean_packet_loss(t) for t in ai_trials]
    r_vals = [mean_packet_loss(t) for t in reactive_trials]
    for i, (label, vals, color) in enumerate([
        ("Active Inference", ai_vals, COLOR_AI),
        ("Reactive", r_vals, COLOR_REACTIVE),
    ]):
        if not vals:
            continue
        x = [i + 1] * len(vals)
        ax.scatter(x, vals, color=color, s=80, alpha=0.7, zorder=3,
                   edgecolors="white", linewidth=0.5)
        m = statistics.mean(vals)
        ax.hlines(m, i + 0.7, i + 1.3, colors=color, linewidth=3, zorder=4,
                  label=f"{label} mean={m:.5f}")
    ax.set_xticks([1, 2])
    ax.set_xticklabels(["Active Inference", "Reactive"])
    ax.set_ylabel("mean packet loss (fraction)")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper right", fontsize=9)
    # Use log scale if values span 2+ orders of magnitude.
    all_vals = [v for v in ai_vals + r_vals if v > 0]
    if all_vals and max(all_vals) / max(min(all_vals), 1e-12) > 100:
        ax.set_yscale("log")
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot F: Aggregate throughput over time ────────────────────────────────


def plot_F_aggregate_throughput(
    scenario_name: str,
    ai_trials: List[TrialSamples],
    reactive_trials: List[TrialSamples],
    out_path: str,
) -> None:
    """Sum of all flows' throughput over time."""
    fig, ax = plt.subplots(1, 1, figsize=(10, 5), constrained_layout=True)
    fig.suptitle(
        f"F. Scenario: {scenario_name} — aggregate throughput over time",
        fontsize=13,
    )
    _plot_timeseries(ax, ai_trials, COLOR_AI, "Active Inference",
                     _aggregate_throughput_over_time)
    _plot_timeseries(ax, reactive_trials, COLOR_REACTIVE, "Reactive",
                     _aggregate_throughput_over_time)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("aggregate throughput (Mbps)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right")
    # Use the same y-axis range for both so they're visually comparable.
    all_vals = []
    for trials in (ai_trials, reactive_trials):
        _, means, _ = _aggregate_throughput_over_time(trials)
        all_vals.extend(means)
    if all_vals:
        ax.set_ylim(0, max(all_vals) * 1.15)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot G: Per-flow throughput ────────────────────────────────────────────


def plot_G_per_flow_throughput(
    scenario_name: str,
    ai_trials: List[TrialSamples],
    reactive_trials: List[TrialSamples],
    out_path: str,
) -> None:
    """One subplot per concurrent flow, overlaying throughput over time.

    Always writes the file — if there are no throughput samples, the
    plot contains a clear placeholder so the output file set is consistent.
    """
    ai_flows = _per_flow_throughput_over_time(ai_trials)
    r_flows = _per_flow_throughput_over_time(reactive_trials)
    all_flow_ids = sorted(set(list(ai_flows.keys()) + list(r_flows.keys())))
    if not all_flow_ids:
        fig, ax = plt.subplots(1, 1, figsize=(8, 3), constrained_layout=True)
        ax.text(0.5, 0.5,
                f"No throughput samples collected for scenario: {scenario_name}\n"
                "(check that iperf3 is installed in Mininet host namespaces)",
                ha="center", va="center", transform=ax.transAxes,
                color="#8b949e", fontsize=11)
        ax.set_xticks([])
        ax.set_yticks([])
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        _log.warning("plot_G: no throughput samples for %s", scenario_name)
        return

    n = len(all_flow_ids)
    n_cols = min(2, n)
    n_rows = (n + n_cols - 1) // n_cols
    fig, axes = plt.subplots(
        n_rows, n_cols, figsize=(7 * n_cols, 4 * n_rows),
        constrained_layout=True,
    )
    if n == 1:
        axes = [axes]
    else:
        axes = axes.flatten() if hasattr(axes, "flatten") else [axes]
    fig.suptitle(
        f"G. Scenario: {scenario_name} — per-flow throughput over time",
        fontsize=13,
    )
    # Compute global y-max across all flows + both approaches for fair axes.
    all_means: List[float] = []
    for fid in all_flow_ids:
        if fid in ai_flows:
            all_means.extend(ai_flows[fid][1])
        if fid in r_flows:
            all_means.extend(r_flows[fid][1])
    y_max = max(all_means) * 1.15 if all_means else 10.0
    for i, fid in enumerate(all_flow_ids):
        ax = axes[i]
        if fid in ai_flows:
            t, m, s = ai_flows[fid]
            ax.plot(t, m, color=COLOR_AI, label="Active Inference", linewidth=1.8)
            ax.fill_between(t, [max(0, a - b) for a, b in zip(m, s)],
                            [a + b for a, b in zip(m, s)],
                            color=COLOR_AI, alpha=0.15)
        if fid in r_flows:
            t, m, s = r_flows[fid]
            ax.plot(t, m, color=COLOR_REACTIVE, label="Reactive", linewidth=1.8)
            ax.fill_between(t, [max(0, a - b) for a, b in zip(m, s)],
                            [a + b for a, b in zip(m, s)],
                            color=COLOR_REACTIVE, alpha=0.15)
        ax.set_title(f"flow {fid}")
        ax.set_xlabel("time (s)")
        ax.set_ylabel("throughput (Mbps)")
        ax.set_ylim(0, y_max)  # same axis for both controllers
        ax.grid(True, alpha=0.3)
        ax.legend(loc="lower right", fontsize=9)
    for j in range(len(all_flow_ids), len(axes)):
        axes[j].set_visible(False)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot H: Jain's fairness (cross-scenario bar chart) ───────────────────


def plot_H_jains_fairness(
    scenario_metrics: Dict[str, Dict[str, ControllerScenarioMetrics]],
    out_path: str,
) -> None:
    """Jain's fairness index per scenario, AI vs Reactive."""
    fig, ax = plt.subplots(1, 1, figsize=(10, 5), constrained_layout=True)
    fig.suptitle("H. Jain's fairness index per scenario", fontsize=13)
    scenarios = sorted(scenario_metrics.keys())
    x = list(range(len(scenarios)))
    bar_width = 0.38
    ai_vals = []
    r_vals = []
    for s in scenarios:
        ai_m = scenario_metrics[s].get("active_inference")
        r_m = scenario_metrics[s].get("reactive")
        ai_vals.append(ai_m.jains_fairness.mean if ai_m else 0.0)
        r_vals.append(r_m.jains_fairness.mean if r_m else 0.0)
    ax.bar([i - bar_width / 2 for i in x], ai_vals, width=bar_width,
           color=COLOR_AI, label="Active Inference")
    ax.bar([i + bar_width / 2 for i in x], r_vals, width=bar_width,
           color=COLOR_REACTIVE, label="Reactive")
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=15, ha="right")
    ax.set_ylabel("Jain's fairness index [1/n .. 1]")
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper right")
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot I: Reroute count (cross-scenario bar chart) ──────────────────────


def plot_I_reroute_count(
    scenario_metrics: Dict[str, Dict[str, ControllerScenarioMetrics]],
    out_path: str,
) -> None:
    """Total reroute events per scenario, AI vs Reactive."""
    fig, ax = plt.subplots(1, 1, figsize=(10, 5), constrained_layout=True)
    fig.suptitle("I. Total reroute events per scenario", fontsize=13)
    scenarios = sorted(scenario_metrics.keys())
    x = list(range(len(scenarios)))
    bar_width = 0.38
    ai_vals = []
    r_vals = []
    ai_succ = []
    r_succ = []
    for s in scenarios:
        ai_m = scenario_metrics[s].get("active_inference")
        r_m = scenario_metrics[s].get("reactive")
        ai_vals.append(ai_m.reroute_count.mean if ai_m else 0.0)
        r_vals.append(r_m.reroute_count.mean if r_m else 0.0)
        ai_succ.append(ai_m.successful_reroutes.mean if ai_m else 0.0)
        r_succ.append(r_m.successful_reroutes.mean if r_m else 0.0)
    # Stacked: total (light) + successful (dark).
    ax.bar([i - bar_width / 2 for i in x], ai_vals, width=bar_width,
           color=COLOR_AI, alpha=0.4, label="AI: total events")
    ax.bar([i - bar_width / 2 for i in x], ai_succ, width=bar_width,
           color=COLOR_AI, label="AI: successful reroutes")
    ax.bar([i + bar_width / 2 for i in x], r_vals, width=bar_width,
           color=COLOR_REACTIVE, alpha=0.4, label="R: total events")
    ax.bar([i + bar_width / 2 for i in x], r_succ, width=bar_width,
           color=COLOR_REACTIVE, label="R: successful reroutes")
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=15, ha="right")
    ax.set_ylabel("events per trial (mean)")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper right", fontsize=9)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot J: Time to recover (cross-scenario bar chart) ────────────────────


def plot_J_time_to_recover(
    scenario_metrics: Dict[str, Dict[str, ControllerScenarioMetrics]],
    out_path: str,
) -> None:
    """Time-to-recover from threshold crossing, AI vs Reactive."""
    fig, ax = plt.subplots(1, 1, figsize=(10, 5), constrained_layout=True)
    fig.suptitle("J. Time to recover from congestion (s)", fontsize=13)
    scenarios = sorted(scenario_metrics.keys())
    x = list(range(len(scenarios)))
    bar_width = 0.38
    ai_vals = []
    r_vals = []
    ai_none = []
    r_none = []
    for s in scenarios:
        ai_m = scenario_metrics[s].get("active_inference")
        r_m = scenario_metrics[s].get("reactive")
        # time_to_recover mean is NaN if all trials had None.
        import math
        ai_vals.append(
            ai_m.time_to_recover.mean if ai_m and not math.isnan(ai_m.time_to_recover.mean) else 0.0
        )
        r_vals.append(
            r_m.time_to_recover.mean if r_m and not math.isnan(r_m.time_to_recover.mean) else 0.0
        )
        ai_none.append(ai_m.time_to_recover.n_none if ai_m else 0)
        r_none.append(r_m.time_to_recover.n_none if r_m else 0)
    ax.bar([i - bar_width / 2 for i in x], ai_vals, width=bar_width,
           color=COLOR_AI, label="Active Inference")
    ax.bar([i + bar_width / 2 for i in x], r_vals, width=bar_width,
           color=COLOR_REACTIVE, label="Reactive")
    # Annotate None counts (trials that never crossed the threshold).
    for i, (an, rn) in enumerate(zip(ai_none, r_none)):
        if an > 0:
            ax.text(i - bar_width / 2, ai_vals[i] + 0.3,
                    f"{an} NC", ha="center", fontsize=8, color=COLOR_AI)
        if rn > 0:
            ax.text(i + bar_width / 2, r_vals[i] + 0.3,
                    f"{rn} NC", ha="center", fontsize=8, color=COLOR_REACTIVE)
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=15, ha="right")
    ax.set_ylabel("time to recover (s)")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper right")
    ax.text(0.5, 0.95, "NC = N trials Never Crossed threshold (excluded from mean)",
            transform=ax.transAxes, ha="center", fontsize=8, color="#8b949e")
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    _log.info("wrote %s", out_path)


# ── Plot helper: time-series with shaded std band ──────────────────────────


def _plot_timeseries(ax, trials, color, label, ts_fn) -> None:
    if not trials:
        return
    t, means, stds = ts_fn(trials)
    if not t:
        return
    ax.plot(t, means, color=color, label=label, linewidth=1.8)
    ax.fill_between(
        t,
        [max(0, m - s) for m, s in zip(means, stds)],
        [m + s for m, s in zip(means, stds)],
        color=color,
        alpha=0.15,
    )


# ── Summary markdown table ────────────────────────────────────────────────


def write_summary_markdown(
    scenario_metrics: Dict[str, Dict[str, ControllerScenarioMetrics]],
    out_path: str,
) -> None:
    """Write the comparison summary as markdown."""
    import math
    lines: List[str] = []
    lines.append("# SDN controller benchmark — comparison summary\n")
    lines.append(
        "All values are means across trials; `±` is one standard deviation.\n"
        "Direction column states which side a metric *typically* favors at "
        "this scenario's load level (see `benchmark/metrics.py` for the "
        "rationale — it's a design-bias heuristic, not a guarantee).\n"
    )
    lines.append(
        "**Key:** AI = Active Inference, R = Reactive, tie = no expected "
        "difference at this load level, NC = trials that Never Crossed "
        "the threshold (excluded from the metric).\n"
    )
    lines.append(
        "**NaN policy:** metrics that are genuinely undefined (e.g. "
        "time-to-recover when no threshold crossing happened) are reported "
        "as `—`, not as `0`. Zero is reserved for actual measured zero.\n"
    )

    for scenario in sorted(scenario_metrics.keys()):
        ai_m = scenario_metrics[scenario].get("active_inference")
        r_m = scenario_metrics[scenario].get("reactive")
        load = classify_scenario_load(scenario)
        lines.append(f"\n## Scenario: `{scenario}` (load class: {load})\n")
        if ai_m is None or r_m is None:
            lines.append(
                "_Incomplete data — run both controllers for this "
                "scenario before comparing._\n"
            )
            continue
        lines.append(
            f"_AI trials: {ai_m.n_trials}, Reactive trials: {r_m.n_trials}_\n"
        )
        lines.append(
            "| Metric | Active Inference | Reactive | Direction | Favors (this load) |"
        )
        lines.append("|---|---|---|---|---|")
        for k, label in METRIC_LABELS.items():
            ai_v = getattr(ai_m, k)
            r_v = getattr(r_m, k)
            direction = METRIC_DIRECTION[k]
            favors = METRIC_FAVORS_AT_LOAD[k][load]
            ai_str = _fmt_metric(k, ai_v)
            r_str = _fmt_metric(k, r_v)
            favors_str = {
                "active_inference": "AI",
                "reactive": "R",
                "tie": "tie",
            }[favors]
            lines.append(
                f"| {label} | {ai_str} | {r_str} | {direction} | {favors_str} |"
            )

        lines.append("")
        lines.append(_tradeoff_narrative(scenario, ai_m, r_m, load))

    with open(out_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    _log.info("wrote %s", out_path)


def _fmt_metric(metric_key: str, s) -> str:
    """Format a MetricStats as a string."""
    import math
    if s.n_trials == 0:
        return "—"
    if math.isnan(s.mean):
        # All trials had undefined values.
        return f"— (n_none={s.n_none})"
    if metric_key in ("reroute_count", "successful_reroutes",
                      "failed_reroutes", "congestion_events"):
        nc_str = f"; {s.n_none} NC" if s.n_none > 0 else ""
        return f"{s.mean:.1f} ± {s.std:.1f} (n={s.n_trials}{nc_str})"
    if metric_key in ("time_to_first_reroute", "time_to_recover") and s.n_none > 0:
        return (
            f"{s.mean:.2f} ± {s.std:.2f} (n={s.n_trials}; "
            f"{s.n_none} NC)"
        )
    return f"{s.mean:.4f} ± {s.std:.4f} (n={s.n_trials})"


def _tradeoff_narrative(scenario, ai_m, r_m, load) -> str:
    """One-paragraph honest tradeoff summary for one scenario."""
    parts: List[str] = []
    ai_rer = ai_m.reroute_count.mean
    r_rer = r_m.reroute_count.mean
    ai_succ = ai_m.successful_reroutes.mean
    r_succ = r_m.successful_reroutes.mean
    ai_fail = ai_m.failed_reroutes.mean
    r_fail = r_m.failed_reroutes.mean
    ai_p95 = ai_m.p95_link_util.mean
    r_p95 = r_m.p95_link_util.mean
    ai_loss = ai_m.mean_packet_loss.mean
    r_loss = r_m.mean_packet_loss.mean
    ai_ttr = ai_m.time_to_recover.mean
    r_ttr = r_m.time_to_recover.mean
    ai_ttr_none = ai_m.time_to_recover.n_none
    r_ttr_none = r_m.time_to_recover.n_none
    ai_ce = ai_m.congestion_events.mean
    r_ce = r_m.congestion_events.mean
    import math
    ai_ttr_str = "—" if math.isnan(ai_ttr) else f"{ai_ttr:.2f}s"
    r_ttr_str = "—" if math.isnan(r_ttr) else f"{r_ttr:.2f}s"

    if load == "low_load":
        parts.append(
            f"**Low-load scenario.** Under `{scenario}`, there is no "
            f"sustained congestion to react to. Reactive's "
            f"reroute_count (R={r_rer:.1f}; successful={r_succ:.1f}, "
            f"failed={r_fail:.1f}) is *legitimately* expected to be low — "
            f"Reactive's design is to do nothing below the 0.45 threshold, "
            f"and that's the correct call here. AI's higher count "
            f"(AI={ai_rer:.1f}; successful={ai_succ:.1f}, "
            f"failed={ai_fail:.1f}) reflects its anticipatory splits — "
            f"this is the cost of being proactive, not a defect."
        )
        parts.append(
            f"Packet loss: AI={ai_loss:.5f} vs R={r_loss:.5f} — both "
            f"should be near zero. Any non-zero loss here is noise."
        )
        parts.append(
            f"Congestion events (threshold crossings): AI={ai_ce:.1f} vs "
            f"R={r_ce:.1f}. Both should be 0 in a true low-load scenario; "
            f"non-zero values indicate transient bursts above threshold."
        )
        parts.append(
            f"Time-to-recover: AI={ai_ttr_str} (NC={ai_ttr_none}), "
            f"R={r_ttr_str} (NC={r_ttr_none}). NC trials never crossed "
            f"the threshold — they're excluded from the mean, not counted "
            f"as 0s."
        )
    else:
        parts.append(
            f"**High-load scenario.** `{scenario}` is designed to push the "
            f"bottleneck link past the 0.45 threshold. Active Inference is "
            f"expected to do better on the load-sensitive metrics:"
        )
        parts.append(
            f"- **P95 link utilisation:** AI={ai_p95:.3f} vs R={r_p95:.3f} — "
            f"lower is better; AI should stay closer to the threshold "
            f"without crossing it."
        )
        parts.append(
            f"- **Packet loss:** AI={ai_loss:.5f} vs R={r_loss:.5f} — lower "
            f"is better; AI's anticipatory splitting should reduce tail loss."
        )
        parts.append(
            f"- **Congestion events:** AI={ai_ce:.1f} vs R={r_ce:.1f} — "
            f"lower is better; AI should keep links below threshold more often."
        )
        parts.append(
            f"- **Time-to-recover:** AI={ai_ttr_str} (NC={ai_ttr_none}) vs "
            f"R={r_ttr_str} (NC={r_ttr_none}) — lower is better. NC trials "
            f"never crossed the threshold; they're not counted in the mean."
        )
        parts.append(
            f"- **Reroute events:** AI total={ai_rer:.1f} "
            f"(successful={ai_succ:.1f}, failed={ai_fail:.1f}) vs "
            f"R total={r_rer:.1f} (successful={r_succ:.1f}, "
            f"failed={r_fail:.1f}). Under load, a higher reroute count for "
            f"AI is a *positive* signal (active load management). Reactive's "
            f"failed reroute count (R={r_fail:.1f}) indicates how often it "
            f"saw congestion but couldn't find a clearly-better path — "
            f"that's a meaningful signal of how 'stuck' it was."
        )
    return "\n".join(parts)


# ── Top-level orchestrator ────────────────────────────────────────────────


def generate_comparison_plots(
    trials_by_approach: Dict[str, Dict[str, List[TrialSamples]]],
    out_dir: str,
) -> Dict[str, str]:
    """
    Generate all plots A-J + markdown summary for a set of trials.

    Returns a dict of {plot_name: file_path}.
    """
    os.makedirs(out_dir, exist_ok=True)
    out_files: Dict[str, str] = {}

    scenarios = set()
    for approach, scenario_map in trials_by_approach.items():
        scenarios.update(scenario_map.keys())
    scenario_metrics: Dict[str, Dict[str, ControllerScenarioMetrics]] = {}

    for scenario in sorted(scenarios):
        ai_trials = trials_by_approach.get("active_inference", {}).get(scenario, [])
        r_trials = trials_by_approach.get("reactive", {}).get(scenario, [])

        # Per-scenario plots A-G.
        p = os.path.join(out_dir, f"A_reroute_events_{scenario}.png")
        plot_A_reroute_events(scenario, ai_trials, r_trials, p)
        out_files[f"A_reroute_events_{scenario}"] = p

        p = os.path.join(out_dir, f"B_max_link_util_{scenario}.png")
        plot_B_max_link_util(scenario, ai_trials, r_trials, p)
        out_files[f"B_max_link_util_{scenario}"] = p

        p = os.path.join(out_dir, f"C_mean_link_util_{scenario}.png")
        plot_C_mean_link_util(scenario, ai_trials, r_trials, p)
        out_files[f"C_mean_link_util_{scenario}"] = p

        p = os.path.join(out_dir, f"D_p95_util_{scenario}.png")
        plot_D_p95_util(scenario, ai_trials, r_trials, p)
        out_files[f"D_p95_util_{scenario}"] = p

        p = os.path.join(out_dir, f"E_packet_loss_{scenario}.png")
        plot_E_packet_loss(scenario, ai_trials, r_trials, p)
        out_files[f"E_packet_loss_{scenario}"] = p

        p = os.path.join(out_dir, f"F_aggregate_throughput_{scenario}.png")
        plot_F_aggregate_throughput(scenario, ai_trials, r_trials, p)
        out_files[f"F_aggregate_throughput_{scenario}"] = p

        p = os.path.join(out_dir, f"G_per_flow_throughput_{scenario}.png")
        plot_G_per_flow_throughput(scenario, ai_trials, r_trials, p)
        out_files[f"G_per_flow_throughput_{scenario}"] = p

        # Aggregate metrics per approach per scenario.
        scenario_metrics[scenario] = {}
        if ai_trials:
            scenario_metrics[scenario]["active_inference"] = aggregate_metrics(ai_trials)
        if r_trials:
            scenario_metrics[scenario]["reactive"] = aggregate_metrics(r_trials)

    # Cross-scenario bar charts H, I, J.
    p = os.path.join(out_dir, "H_jains_fairness.png")
    plot_H_jains_fairness(scenario_metrics, p)
    out_files["H_jains_fairness"] = p

    p = os.path.join(out_dir, "I_reroute_count.png")
    plot_I_reroute_count(scenario_metrics, p)
    out_files["I_reroute_count"] = p

    p = os.path.join(out_dir, "J_time_to_recover.png")
    plot_J_time_to_recover(scenario_metrics, p)
    out_files["J_time_to_recover"] = p

    # Summary markdown + JSON.
    p = os.path.join(out_dir, "summary.md")
    write_summary_markdown(scenario_metrics, p)
    out_files["summary_md"] = p

    p = os.path.join(out_dir, "summary.json")
    import json
    with open(p, "w") as fh:
        json.dump(
            {
                scenario: {
                    approach: m.to_dict()
                    for approach, m in cmap.items()
                }
                for scenario, cmap in scenario_metrics.items()
            },
            fh,
            indent=2,
        )
    out_files["summary_json"] = p

    return out_files
