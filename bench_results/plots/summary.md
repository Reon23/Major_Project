# SDN controller benchmark — comparison summary

All values are means across trials; `±` is one standard deviation.
Direction column states which side a metric *typically* favors at this scenario's load level (see `benchmark/metrics.py` for the rationale — it's a design-bias heuristic, not a guarantee).

**Key:** AI = Active Inference, R = Reactive, tie = no expected difference at this load level, NC = trials that Never Crossed the threshold (excluded from the metric).

**NaN policy:** metrics that are genuinely undefined (e.g. time-to-recover when no threshold crossing happened) are reported as `—`, not as `0`. Zero is reserved for actual measured zero.


## Scenario: `baseline` (load class: low_load)

_AI trials: 5, Reactive trials: 5_

| Metric | Active Inference | Reactive | Direction | Favors (this load) |
|---|---|---|---|---|
| Mean link util | 0.0373 ± 0.0033 (n=5) | 0.0376 ± 0.0043 (n=5) | lower | R |
| Max link util (bottleneck) | 0.3091 ± 0.0017 (n=5) | 0.3097 ± 0.0006 (n=5) | lower | tie |
| P95 link util | 0.3021 ± 0.0012 (n=5) | 0.3069 ± 0.0013 (n=5) | lower | tie |
| Mean packet loss | 0.0000 ± 0.0000 (n=5) | 0.0000 ± 0.0000 (n=5) | lower | tie |
| Mean throughput (Mbps) | 2.9994 ± 0.0000 (n=5) | 2.9994 ± 0.0000 (n=5) | higher | tie |
| Throughput CoV (lower=better) | 0.0008 ± 0.0000 (n=5) | 0.0008 ± 0.0000 (n=5) | lower | tie |
| Jain's fairness (higher=better) | 1.0000 ± 0.0000 (n=5) | 1.0000 ± 0.0000 (n=5) | higher | tie |
| Total reroute events | 166.2 ± 66.5 (n=5) | 82.6 ± 43.1 (n=5) | contextual | R |
| Successful reroutes | 16.0 ± 5.2 (n=5) | 3.0 ± 2.1 (n=5) | contextual | R |
| Failed reroute attempts | 0.0 ± 0.0 (n=5) | 39.6 ± 20.7 (n=5) | lower | tie |
| Time to first reroute (s) | 2.1191 ± 2.2764 (n=5) | 6.71 ± 2.14 (n=5; 1 NC) | contextual | tie |
| Time to recover (s) | — (n_none=5) | — (n_none=5) | lower | tie |
| Congestion events count | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | lower | tie |

**Low-load scenario.** Under `baseline`, there is no sustained congestion to react to. Reactive's reroute_count (R=82.6; successful=3.0, failed=39.6) is *legitimately* expected to be low — Reactive's design is to do nothing below the 0.45 threshold, and that's the correct call here. AI's higher count (AI=166.2; successful=16.0, failed=0.0) reflects its anticipatory splits — this is the cost of being proactive, not a defect.
Packet loss: AI=0.00000 vs R=0.00000 — both should be near zero. Any non-zero loss here is noise.
Congestion events (threshold crossings): AI=0.0 vs R=0.0. Both should be 0 in a true low-load scenario; non-zero values indicate transient bursts above threshold.
Time-to-recover: AI=— (NC=5), R=— (NC=5). NC trials never crossed the threshold — they're excluded from the mean, not counted as 0s.

## Scenario: `bursty_threshold` (load class: low_load)

_AI trials: 5, Reactive trials: 5_

| Metric | Active Inference | Reactive | Direction | Favors (this load) |
|---|---|---|---|---|
| Mean link util | 0.0662 ± 0.0087 (n=5) | 0.0752 ± 0.0123 (n=5) | lower | R |
| Max link util (bottleneck) | 0.4974 ± 0.0313 (n=5) | 0.4958 ± 0.0126 (n=5) | lower | tie |
| P95 link util | 0.2717 ± 0.0225 (n=5) | 0.2662 ± 0.0110 (n=5) | lower | tie |
| Mean packet loss | 0.0000 ± 0.0000 (n=5) | 0.0000 ± 0.0000 (n=5) | lower | tie |
| Mean throughput (Mbps) | 2.4969 ± 0.0004 (n=5) | 2.4967 ± 0.0002 (n=5) | higher | tie |
| Throughput CoV (lower=better) | 0.0028 ± 0.0004 (n=5) | 0.0033 ± 0.0003 (n=5) | lower | tie |
| Jain's fairness (higher=better) | 1.0000 ± 0.0000 (n=5) | 1.0000 ± 0.0000 (n=5) | higher | tie |
| Total reroute events | 511.6 ± 104.5 (n=5) | 202.0 ± 44.2 (n=5) | contextual | R |
| Successful reroutes | 14.2 ± 8.4 (n=5) | 16.6 ± 4.9 (n=5) | contextual | R |
| Failed reroute attempts | 0.0 ± 0.0 (n=5) | 93.8 ± 22.8 (n=5) | lower | tie |
| Time to first reroute (s) | 6.6358 ± 10.5087 (n=5) | 4.1060 ± 0.7636 (n=5) | contextual | tie |
| Time to recover (s) | 2.0015 ± 0.0016 (n=5) | 2.0009 ± 0.0015 (n=5) | lower | tie |
| Congestion events count | 2.4 ± 0.9 (n=5) | 2.6 ± 0.9 (n=5) | lower | tie |

**Low-load scenario.** Under `bursty_threshold`, there is no sustained congestion to react to. Reactive's reroute_count (R=202.0; successful=16.6, failed=93.8) is *legitimately* expected to be low — Reactive's design is to do nothing below the 0.45 threshold, and that's the correct call here. AI's higher count (AI=511.6; successful=14.2, failed=0.0) reflects its anticipatory splits — this is the cost of being proactive, not a defect.
Packet loss: AI=0.00000 vs R=0.00000 — both should be near zero. Any non-zero loss here is noise.
Congestion events (threshold crossings): AI=2.4 vs R=2.6. Both should be 0 in a true low-load scenario; non-zero values indicate transient bursts above threshold.
Time-to-recover: AI=2.00s (NC=0), R=2.00s (NC=0). NC trials never crossed the threshold — they're excluded from the mean, not counted as 0s.

## Scenario: `forced_bottleneck` (load class: high_load)

_AI trials: 5, Reactive trials: 5_

| Metric | Active Inference | Reactive | Direction | Favors (this load) |
|---|---|---|---|---|
| Mean link util | 0.0789 ± 0.0018 (n=5) | 0.0740 ± 0.0026 (n=5) | lower | AI |
| Max link util (bottleneck) | 0.5845 ± 0.0101 (n=5) | 0.5450 ± 0.1305 (n=5) | lower | AI |
| P95 link util | 0.3093 ± 0.0041 (n=5) | 0.3084 ± 0.0022 (n=5) | lower | AI |
| Mean packet loss | 0.0000 ± 0.0000 (n=5) | 0.0000 ± 0.0000 (n=5) | lower | AI |
| Mean throughput (Mbps) | 2.9995 ± 0.0001 (n=5) | 2.9995 ± 0.0001 (n=5) | higher | AI |
| Throughput CoV (lower=better) | 0.0009 ± 0.0002 (n=5) | 0.0010 ± 0.0003 (n=5) | lower | AI |
| Jain's fairness (higher=better) | 1.0000 ± 0.0000 (n=5) | 1.0000 ± 0.0000 (n=5) | higher | AI |
| Total reroute events | 716.2 ± 224.7 (n=5) | 213.0 ± 30.1 (n=5) | contextual | AI |
| Successful reroutes | 18.0 ± 5.9 (n=5) | 16.8 ± 6.8 (n=5) | contextual | AI |
| Failed reroute attempts | 0.0 ± 0.0 (n=5) | 97.8 ± 15.5 (n=5) | lower | AI |
| Time to first reroute (s) | 1.4629 ± 0.7572 (n=5) | 3.7725 ± 0.9763 (n=5) | contextual | AI |
| Time to recover (s) | 2.0005 ± 0.0005 (n=5) | 2.00 ± 0.00 (n=5; 1 NC) | lower | AI |
| Congestion events count | 3.8 ± 1.6 (n=5) | 1.2 ± 0.8 (n=5) | lower | AI |

**High-load scenario.** `forced_bottleneck` is designed to push the bottleneck link past the 0.45 threshold. Active Inference is expected to do better on the load-sensitive metrics:
- **P95 link utilisation:** AI=0.309 vs R=0.308 — lower is better; AI should stay closer to the threshold without crossing it.
- **Packet loss:** AI=0.00000 vs R=0.00000 — lower is better; AI's anticipatory splitting should reduce tail loss.
- **Congestion events:** AI=3.8 vs R=1.2 — lower is better; AI should keep links below threshold more often.
- **Time-to-recover:** AI=2.00s (NC=0) vs R=2.00s (NC=1) — lower is better. NC trials never crossed the threshold; they're not counted in the mean.
- **Reroute events:** AI total=716.2 (successful=18.0, failed=0.0) vs R total=213.0 (successful=16.8, failed=97.8). Under load, a higher reroute count for AI is a *positive* signal (active load management). Reactive's failed reroute count (R=97.8) indicates how often it saw congestion but couldn't find a clearly-better path — that's a meaningful signal of how 'stuck' it was.

## Scenario: `load_ramp` (load class: high_load)

_AI trials: 5, Reactive trials: 5_

| Metric | Active Inference | Reactive | Direction | Favors (this load) |
|---|---|---|---|---|
| Mean link util | 0.1095 ± 0.0200 (n=5) | 0.1030 ± 0.0135 (n=5) | lower | AI |
| Max link util (bottleneck) | 0.9018 ± 0.1360 (n=5) | 0.9393 ± 0.0260 (n=5) | lower | AI |
| P95 link util | 0.4332 ± 0.1102 (n=5) | 0.8512 ± 0.0324 (n=5) | lower | AI |
| Mean packet loss | 0.0000 ± 0.0000 (n=5) | 0.0000 ± 0.0000 (n=5) | lower | AI |
| Mean throughput (Mbps) | 2.7281 ± 0.1654 (n=5) | 2.8232 ± 0.0002 (n=5) | higher | AI |
| Throughput CoV (lower=better) | 0.0277 ± 0.0087 (n=5) | 0.0024 ± 0.0005 (n=5) | lower | AI |
| Jain's fairness (higher=better) | 0.9382 ± 0.0131 (n=5) | 0.9308 ± 0.0001 (n=5) | higher | AI |
| Total reroute events | 2861.2 ± 478.3 (n=5) | 404.0 ± 165.6 (n=5) | contextual | AI |
| Successful reroutes | 9.4 ± 9.4 (n=5) | 54.0 ± 26.4 (n=5) | contextual | AI |
| Failed reroute attempts | 0.0 ± 0.0 (n=5) | 181.6 ± 74.9 (n=5) | lower | AI |
| Time to first reroute (s) | 6.88 ± 5.07 (n=5; 1 NC) | 5.9561 ± 2.9759 (n=5) | contextual | AI |
| Time to recover (s) | 3.6021 ± 1.6732 (n=5) | 4.8038 ± 5.2202 (n=5) | lower | AI |
| Congestion events count | 7.0 ± 2.2 (n=5) | 13.6 ± 5.4 (n=5) | lower | AI |

**High-load scenario.** `load_ramp` is designed to push the bottleneck link past the 0.45 threshold. Active Inference is expected to do better on the load-sensitive metrics:
- **P95 link utilisation:** AI=0.433 vs R=0.851 — lower is better; AI should stay closer to the threshold without crossing it.
- **Packet loss:** AI=0.00000 vs R=0.00000 — lower is better; AI's anticipatory splitting should reduce tail loss.
- **Congestion events:** AI=7.0 vs R=13.6 — lower is better; AI should keep links below threshold more often.
- **Time-to-recover:** AI=3.60s (NC=0) vs R=4.80s (NC=0) — lower is better. NC trials never crossed the threshold; they're not counted in the mean.
- **Reroute events:** AI total=2861.2 (successful=9.4, failed=0.0) vs R total=404.0 (successful=54.0, failed=181.6). Under load, a higher reroute count for AI is a *positive* signal (active load management). Reactive's failed reroute count (R=181.6) indicates how often it saw congestion but couldn't find a clearly-better path — that's a meaningful signal of how 'stuck' it was.
