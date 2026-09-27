# SDN Controller Benchmark Harness — Fair Comparison

Standalone benchmarking tool for comparing the **Active Inference** and
**Reactive** Ryu SDN controllers. Reads only what the controllers already
expose — `state.json` and the `FlowRPCServer` socket — without modifying
them. Routing decisions are emitted as structured events via
`sdn/event_log.py` to a JSONL file (the SINGLE source of truth — never
inferred from state.json path diffs).

## Fair-comparison design

The two controllers are evaluated under exactly the same network
conditions: same topology, same flows, same traffic demands, same link
capacities, same simulation duration, same scenarios. The only meaningful
difference is the path-selection / rerouting algorithm.

### Shared structured event log

Both controllers call `sdn.event_log.log_reroute_event(...)` whenever
they make a routing decision. Events are written to a JSONL file
(path from `SDN_EVENT_LOG_PATH` env var, set per-trial by the runner).
Each event captures:

```json
{
  "timestamp": 1234567890.123,
  "trial_id": "active_inference__baseline__trial00",
  "approach": "active_inference",
  "scenario": "baseline",
  "trial": 0,
  "flow_id": "10.0.0.1->10.0.0.2",
  "src_ip": "10.0.0.1",
  "dst_ip": "10.0.0.2",
  "old_path": ["s1", "s2", "s4"],
  "new_path": ["s1", "s3", "s4"],
  "trigger_reason": "congestion_threshold_crossed",
  "bottleneck_util_before": 0.52,
  "bottleneck_util_after": 0.31,
  "alt_path_best_util": 0.31,
  "improvement": 0.21,
  "success": true,
  "approach_specific": { ... }
}
```

**Trigger reasons** (standardised across both controllers):

| Reason | Emitted by | Meaning |
|---|---|---|
| `cold_start` | Both | First path selection for a flow |
| `congestion_threshold_crossed` | Reactive | Threshold crossed + better path found |
| `no_better_path` | Reactive | Threshold crossed but no path improves enough (failed attempt) |
| `ecmp_fallback` | Reactive | Equal-weight ECMP fallback (no clearly-better path) |
| `hysteresis_hold` | Reactive | Cooldown prevented a reroute |
| `efe_switch` | Active Inference | EFE analysis recommended switching paths |
| `efe_split` | Active Inference | EFE analysis recommended multipath split |

### Reactive controller improvements

The Reactive controller now:
- **Logs cold_start** for every flow's initial path (so the benchmark
  has a complete decision record from both controllers).
- **Logs failed reroute attempts** as `no_better_path` events (success=false)
  when threshold is crossed but no alternative improves by
  `REACTIVE_MIN_IMPROVEMENT`.
- **Logs hysteresis holds** as `hysteresis_hold` events (success=false)
  when cooldown prevents a reroute.
- **Counts congestion events** per flow (transitions from below-threshold
  to at/above-threshold).
- **Has explicit cooldown** (`REACTIVE_REROUTE_COOLDOWN_TICKS = 3` ticks,
  ≈ 6s at POLL_INTERVAL=2s) to prevent flap cycles.

The routing algorithm itself is unchanged — it still only reroutes when
the threshold is genuinely crossed AND a clearly-better path exists. If
Reactive legitimately doesn't reroute in a scenario, that's a real result
(zero reroutes) and the plots will show it honestly alongside non-zero
throughput / utilisation / loss measurements.

## Metrics collected (per trial, per (approach, scenario))

For BOTH Active Inference and Reactive:

- **Mean link utilisation** (across all switch-switch links, all time)
- **Max / bottleneck link utilisation**
- **P95 link utilisation**
- **Mean packet loss** (across all links, all time)
- **Mean throughput** (across all flow_stats events)
- **Throughput CoV** (coefficient of variation, averaged across flows)
- **Jain's fairness index** across concurrent flows
- **Total reroute events** (raw count of every routing-decision event)
- **Successful reroutes** (events where path actually changed)
- **Failed reroute attempts** (`no_better_path` + `hysteresis_hold`)
- **Time to first reroute** (seconds from trial start to first successful switch)
- **Time to recover** (seconds from threshold crossing to first drop below)
- **Congestion events count** (number of threshold crossings observed)

### NaN vs zero policy

Metrics that are genuinely *undefined* for a trial (e.g. "time to first
reroute" when there were no reroutes) are recorded as `None` (JSON `null`),
NOT `0.0`. The aggregation step tracks how many trials had `None` for each
metric, and the summary report says "(N trials had no reroutes — undefined)"
instead of silently treating that as "0s".

Zero is reserved for actual measured zero (e.g. zero packet loss in a
low-load trial is a real measurement, not undefined).

## Output structure

```
bench_results/
├── results/
│   ├── baseline/
│   │   ├── active_inference/
│   │   │   └── raw/
│   │   │       ├── trial00.json              (raw samples)
│   │   │       ├── trial00_events.jsonl      (structured reroute events)
│   │   │       ├── trial01.json
│   │   │       ├── trial01_events.jsonl
│   │   │       └── ...
│   │   └── reactive/
│   │       └── raw/
│   │           └── ...
│   ├── forced_bottleneck/
│   │   ├── active_inference/raw/...
│   │   └── reactive/raw/...
│   ├── load_ramp/...
│   ├── bursty_threshold/...
│   └── _trial_metadata.json     (current trial's metadata, read by controller)
└── plots/
    ├── A_reroute_events_<scenario>.png       (per-scenario, AI vs Reactive)
    ├── B_max_link_util_<scenario>.png         (per-scenario, with threshold line)
    ├── C_mean_link_util_<scenario>.png        (per-scenario)
    ├── D_p95_util_<scenario>.png              (per-scenario)
    ├── E_packet_loss_<scenario>.png           (per-scenario)
    ├── F_aggregate_throughput_<scenario>.png  (per-scenario)
    ├── G_per_flow_throughput_<scenario>.png   (per-scenario, equivalent axes)
    ├── H_jains_fairness.png                   (cross-scenario bar)
    ├── I_reroute_count.png                    (cross-scenario bar, stacked total+successful)
    ├── J_time_to_recover.png                  (cross-scenario bar, with NC annotations)
    ├── summary.md                             (narrative tradeoff analysis)
    └── summary.json                           (machine-readable aggregates)
```

## Sanity checks (run before any plot is generated)

The harness runs `benchmark/sanity_check.py` before plotting. Checks:

1. Both approaches have the same number of trials per scenario.
2. Both approaches have the same flow IDs (so per-flow plots are comparable).
3. Both approaches cover the same simulation time range (within 10s).
4. Both approaches have throughput samples (else per-flow plots are empty).
5. Both approaches have link utilisation samples.
6. Packet loss is measured consistently.
7. Jain's fairness is calculated over the same flows.
8. Reroute event timestamps are within the trial window.
9. Path changes in successful reroute events actually correspond to different
   paths (else the controller logged a no-op as a reroute — flagged as a warning).
10. The 0.45 threshold is consistent between controllers (`REACTIVE_CONGESTION_THRESHOLD`
    must equal `MULTIPATH_CONGESTION_THRESHOLD`).

**Errors abort plot generation** — the user must fix the measurement problem
before any plot can be trusted. **Warnings** are logged but plotting continues.

## Quick start

```bash
# Full benchmark — one command, auto-starts ryu + mininet itself
python3 run_benchmark.py run --trials 3

# Quick smoke test: 1 trial, 2 scenarios
python3 run_benchmark.py run --trials 1 --scenarios baseline,forced_bottleneck

# Provide sudo password via env var (no prompt)
SDN_SUDO_PASSWORD=mypass python3 run_benchmark.py run --trials 3

# Manual mode (you've already started ryu + mininet — old behaviour,
# useful for debugging one layer in isolation)
python3 run_benchmark.py run --manual --trials 3

# Regenerate plots from saved trials (no network needed)
python3 run_benchmark.py compare

# List saved trials
python3 run_benchmark.py list

# List predefined scenarios
python3 run_benchmark.py scenarios
```

## Predefined scenarios

(`python3 run_benchmark.py scenarios` for the canonical list)

| Scenario | What it tests | Expected winner |
|---|---|---|
| `baseline` | One flow at 3 Mbps, far below 0.45. Sanity check. | Tie (both should deliver full throughput with zero loss) |
| `forced_bottleneck` | Two flows share a bottleneck, individually below threshold, combined above. | Active Inference (anticipatory split) |
| `load_ramp` | 3 flows started 5 s apart with increasing rates; threshold crossed mid-scenario. | Active Inference (smoother utilisation under changing load) |
| `bursty_threshold` | Two flows hover at threshold with on/off timing — path-flap stress test. | Active Inference (multipath bias prevents flapping) |

## Honest tradeoff interpretation

The summary report (`summary.md`) explicitly states which controller each
metric *typically* favors at the scenario's load level (a design-bias
heuristic, not a guarantee). The measured values determine the actual
verdict.

**Important:** a zero reroute count for Reactive is a *real result* when
the scenario doesn't push a link past the threshold — Reactive's design
is to do nothing below threshold, and that's the correct call. The plots
will show zero reroutes for Reactive alongside non-zero throughput,
utilisation, and packet-loss measurements, so the comparison is still
fair on those metrics.

Conversely, under high load, a higher reroute count for Active Inference
is a *positive* signal (active load management), not a defect. Reactive's
`failed_reroutes` count (threshold crossed but no better path found) is
a meaningful signal of how "stuck" it was on a congested path.

## Files

```
benchmark/
├── __init__.py
├── scenarios.py        — 4 predefined traffic patterns
├── rpc_client.py       — JSON-line TCP client for FlowRPCServer
├── state_poller.py     — atomic reader for state.json
├── recorder.py         — per-trial sample collector (reads structured events)
├── metrics.py          — 13 metrics + honesty heuristics + NaN policy
├── sanity_check.py     — 10 consistency checks before plotting
├── plotter.py          — plots A-J + summary markdown
├── runner.py           — manual-mode orchestrator
└── auto_runner.py      — one-command auto-start orchestrator (default)

sdn/event_log.py        — shared structured reroute event logger (used by BOTH controllers)

run_benchmark.py        — CLI entry point (run / compare / list / scenarios)
```

## Requirements

Already in the repo's `requirements.txt`:
- `matplotlib>=3.9`
- `ryu`, `mininet` (runtime; the harness shells out to `ryu-manager` and `topology.py`)
- standard library

No new dependencies added.
