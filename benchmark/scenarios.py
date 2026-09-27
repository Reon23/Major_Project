"""
benchmark/scenarios.py — Predefined traffic patterns for the comparison.

Each scenario is a list of FlowStep entries with explicit (src, dst,
proto, bw, start_offset, duration) — plus a `monitor_duration` that
bounds how long the recorder collects samples after the last flow
starts (must be >= longest flow so we capture both the steady state
and the post-flow cooldown).

The four scenarios below were chosen to expose specific controller
behaviour:

  baseline       — one flow at moderate rate, far below the 0.45
                   threshold. Sanity check that both controllers move
                   traffic without weird artefacts; baseline throughput
                   and zero loss are expected for both.

  forced_bottleneck — two flows that share a bottleneck link, each
                   individually below the threshold but together
                   pushing the shared link past 0.45. Distinguishes
                   *anticipatory* splitting (Active Inference) from
                   *wait-then-react* (Reactive).

  load_ramp      — staggered start of 3 flows at increasing rates so
                   the bottleneck crosses the threshold mid-scenario.
                   Tests how each controller behaves under a *changing*
                   load profile (not just steady-state).

  bursty_threshold — two flows that hover right around the 0.45 line
                   with on/off timing. This is the path-flap stress
                   test — a controller that over-confidently commits
                   to one path will flap; one that hedges via multipath
                   should stay smooth.

All host names refer to the default topology_spec.json (h1..h10,
s1..s8). They can be overridden by passing a custom Scenario to the
runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class FlowStep:
    """One flow command in a scenario, scheduled at `start_offset` seconds."""

    flow_id: str          # unique within a trial
    src: str              # e.g. "h1"
    dst: str              # e.g. "h2"
    proto: str = "udp"    # "udp" or "tcp"
    bandwidth_mbps: float = 5.0
    start_offset: float = 0.0   # seconds relative to trial start
    duration_s: int = 20        # iperf3 -t

    def to_rpc_params(self) -> dict:
        """Convert to the dict expected by FlowRPCServer.start_flow."""
        return {
            "id": self.flow_id,
            "src": self.src,
            "dst": self.dst,
            "proto": self.proto,
            "bandwidth_mbps": self.bandwidth_mbps,
            "duration_s": self.duration_s,
        }


@dataclass
class Scenario:
    """A named traffic pattern + monitoring window."""

    name: str
    description: str
    steps: List[FlowStep]
    # How long to keep recording samples after the trial starts. Must
    # be >= the latest (step.start_offset + step.duration_s); a 5s tail
    # is appended automatically inside the recorder so the post-flow
    # state settles and any final `flow_finished` events arrive.
    monitor_duration: float = 25.0

    @property
    def latest_flow_end(self) -> float:
        if not self.steps:
            return 0.0
        return max(s.start_offset + s.duration_s for s in self.steps)

    def __post_init__(self) -> None:
        # Ensure monitor_duration is at least latest_flow_end + 5s tail.
        min_needed = self.latest_flow_end + 5.0
        if self.monitor_duration < min_needed:
            self.monitor_duration = min_needed


# ── The four predefined scenarios ──────────────────────────────────────────
#
# Default topology (topology_spec.json) has these switch-switch bottleneck
# links all at 10 Mbps, 10 ms delay, max_queue_size 50:
#   s1-s2, s1-s3, s2-s4, s3-s4, s3-s5, s4-s5, s3-s6, s2-s6, s2-s8,
#   s1-s8, s1-s7, s4-s7
# Host attachments:
#   h1->s1, h2->s4, h3->s2, h4->s2, h5->s3, h6->s3, h7->s5, h8->s7,
#   h9->s6, h10->s8
#
# 0.45 * 10 Mbps = 4.5 Mbps — that's the congestion threshold on a
# 10 Mbps trunk link.

SCENARIOS: List[Scenario] = [
    # ── 1. Baseline: no contention ─────────────────────────────────────
    Scenario(
        name="baseline",
        description=(
            "Single UDP flow at 3 Mbps — well below the 0.45 utilisation "
            "threshold (4.5 Mbps on a 10 Mbps trunk). Sanity check: both "
            "controllers should deliver full throughput with zero loss "
            "and zero reroutes."
        ),
        steps=[
            FlowStep(
                flow_id="base",
                src="h1",
                dst="h2",
                proto="udp",
                bandwidth_mbps=3.0,
                start_offset=2.0,
                duration_s=15,
            ),
        ],
        monitor_duration=22.0,
    ),

    # ── 2. Two-flow forced bottleneck ──────────────────────────────────
    Scenario(
        name="forced_bottleneck",
        description=(
            "Two UDP flows start simultaneously, each at 3 Mbps, sharing "
            "the same bottleneck link (h1->h2 and h3->h4 both transit "
            "s1-s2-s4 vs s1-s3-s4 paths). Individually below the 0.45 "
            "threshold, combined above it — distinguishes anticipatory "
            "splitting (Active Inference) from wait-then-react (Reactive)."
        ),
        steps=[
            FlowStep(
                flow_id="fb_a",
                src="h1",
                dst="h2",
                proto="udp",
                bandwidth_mbps=3.0,
                start_offset=2.0,
                duration_s=20,
            ),
            FlowStep(
                flow_id="fb_b",
                src="h3",
                dst="h5",
                proto="udp",
                bandwidth_mbps=3.0,
                start_offset=2.5,
                duration_s=20,
            ),
        ],
        monitor_duration=28.0,
    ),

    # ── 3. Staggered load ramp past the threshold ─────────────────────
    Scenario(
        name="load_ramp",
        description=(
            "Three flows started 5 seconds apart with increasing rates "
            "(2 Mbps → 3 Mbps → 4 Mbps), all sharing the s1-s2-s4 path. "
            "Crosses the 0.45 threshold mid-scenario — tests how each "
            "controller behaves under a *changing* load profile, not "
            "just steady state."
        ),
        steps=[
            FlowStep(
                flow_id="ramp_1",
                src="h1",
                dst="h2",
                proto="udp",
                bandwidth_mbps=2.0,
                start_offset=2.0,
                duration_s=22,
            ),
            FlowStep(
                flow_id="ramp_2",
                src="h1",
                dst="h2",
                proto="udp",
                bandwidth_mbps=3.0,
                start_offset=7.0,
                duration_s=17,
            ),
            FlowStep(
                flow_id="ramp_3",
                src="h1",
                dst="h2",
                proto="udp",
                bandwidth_mbps=4.0,
                start_offset=12.0,
                duration_s=12,
            ),
        ],
        monitor_duration=30.0,
    ),

    # ── 4. Bursty near-threshold (path-flap stress test) ───────────────
    Scenario(
        name="bursty_threshold",
        description=(
            "Two flows whose combined rate hovers right at the 0.45 "
            "threshold, with on/off bursty timing. This is the path-flap "
            "stress test — a controller that over-confidently commits to "
            "one path will flap between alternatives; one that hedges via "
            "multipath (Active Inference's 'split' decision) should stay "
            "smooth."
        ),
        steps=[
            FlowStep(
                flow_id="burst_a1",
                src="h1",
                dst="h2",
                proto="udp",
                bandwidth_mbps=2.5,
                start_offset=2.0,
                duration_s=8,
            ),
            FlowStep(
                flow_id="burst_b1",
                src="h3",
                dst="h5",
                proto="udp",
                bandwidth_mbps=2.5,
                start_offset=4.0,
                duration_s=8,
            ),
            FlowStep(
                flow_id="burst_a2",
                src="h1",
                dst="h2",
                proto="udp",
                bandwidth_mbps=2.5,
                start_offset=12.0,
                duration_s=8,
            ),
            FlowStep(
                flow_id="burst_b2",
                src="h3",
                dst="h5",
                proto="udp",
                bandwidth_mbps=2.5,
                start_offset=14.0,
                duration_s=8,
            ),
        ],
        monitor_duration=28.0,
    ),
]


def get_scenario(name: str) -> Optional[Scenario]:
    """Look up a scenario by name. Returns None if not found."""
    for s in SCENARIOS:
        if s.name == name:
            return s
    return None
