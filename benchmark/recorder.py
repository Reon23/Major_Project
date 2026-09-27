"""
benchmark/recorder.py — Per-trial sample collection.

A TrialRecorder runs one scenario trial:
  1. starts a StatePoller (state.json at POLL_INTERVAL cadence)
  2. opens a FlowRPCClient (drains events from the FlowRPCServer)
  3. sets up the per-trial SDN_EVENT_LOG_PATH env var so the controller
     writes structured reroute events to a per-trial JSONL file
  4. executes the scenario's FlowStep schedule (start_flow / stop_flow
     at the offsets defined by the scenario)
  5. waits for `monitor_duration` seconds, polling state.json and
     collecting flow_stats events
  6. reads the structured reroute events JSONL file (single source of
     truth for routing decisions — never inferred from state.json diffs)
  7. returns a TrialSamples payload

The TrialSamples dataclass is a plain Python object that serialises
to JSON — see to_dict() / from_dict(). Output is structured under
results/<scenario>/<approach>/raw/trialNN.json.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from benchmark.rpc_client import FlowRPCClient
from benchmark.scenarios import Scenario
from benchmark.state_poller import StatePoller
from sdn import event_log as sdn_event_log

_log = logging.getLogger(__name__)


# ── Sample data structures ─────────────────────────────────────────────────


@dataclass
class LinkSample:
    """One link's utilisation at one point in time."""

    t: float                # seconds since trial start
    src: str                # "s1"
    dst: str                # "s2"
    util: float             # 0..1
    rate_mbps: float        # measured rate
    capacity_mbps: float    # link capacity
    loss_fraction: float    # 0..1


@dataclass
class FlowSample:
    """One flow's state at one point in time (from state.json's flows)."""

    t: float
    src_ip: str
    dst_ip: str
    path: List[str]         # ["s1", "s2", "s4"]
    G: float                # expected free energy (0 for Reactive)
    rerouted: bool


@dataclass
class ThroughputSample:
    """One iperf3 throughput reading for one flow (from flow_stats events)."""

    t: float                # wall-clock time of event arrival
    flow_id: str
    mbps: float


@dataclass
class RerouteEvent:
    """
    A structured routing decision event from the controller.

    Read from the JSONL log file written by sdn.event_log.log_reroute_event
    (called by both active_inference_dynamic.py and reactive_dynamic.py).
    This is the SINGLE source of truth for routing decisions — never
    inferred from state.json path diffs (which miss intermediate changes
    and have no metadata).
    """

    t: float                          # seconds since trial start
    wall_time: float                  # epoch seconds (original timestamp)
    flow_id: str
    src_ip: str
    dst_ip: str
    old_path: List[str]
    new_path: List[str]
    trigger_reason: str               # cold_start / efe_switch / efe_split /
                                      # congestion_threshold_crossed /
                                      # no_better_path / ecmp_fallback /
                                      # hysteresis_hold
    bottleneck_util_before: Optional[float]
    bottleneck_util_after: Optional[float]
    alt_path_best_util: Optional[float]
    improvement: Optional[float]
    success: bool
    approach_specific: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TrialSamples:
    """All collected samples for one trial."""

    controller: str             # "active_inference" or "reactive"
    approach: str               # alias of controller (clearer name in plots)
    scenario: str
    trial: int
    started_at: float
    finished_at: float
    # Per-trial metadata about the trial conditions (for sanity checks).
    flow_ids: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # flow_ids maps flow_id -> {"src": "h1", "dst": "h2", ...}
    # ONLY contains real benchmark flows — never _warmup_* flows.
    notes: str = ""

    # Time-series samples (warmup flows are EXCLUDED — they're
    # infrastructure, not benchmark measurements):
    link_samples: List[LinkSample] = field(default_factory=list)
    flow_samples: List[FlowSample] = field(default_factory=list)
    throughput_samples: List[ThroughputSample] = field(default_factory=list)

    # Structured routing decision events (from the JSONL log):
    reroute_events: List[RerouteEvent] = field(default_factory=list)

    # Per-flow start_flow results, for diagnostics. Each entry is:
    #   {"flow_id": str, "src": str, "dst": str, "ok": bool, "error": str|None,
    #    "started_at": float}
    # If a flow failed to start, the error is preserved here so the user
    # can see WHY throughput is zero instead of just seeing empty samples.
    flow_start_results: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "controller": self.controller,
            "approach": self.approach,
            "scenario": self.scenario,
            "trial": self.trial,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.finished_at - self.started_at, 3),
            "flow_ids": self.flow_ids,
            "notes": self.notes,
            "flow_start_results": self.flow_start_results,
            "link_samples": [
                {"t": s.t, "src": s.src, "dst": s.dst, "util": s.util,
                 "rate_mbps": s.rate_mbps, "capacity_mbps": s.capacity_mbps,
                 "loss_fraction": s.loss_fraction}
                for s in self.link_samples
            ],
            "flow_samples": [
                {"t": s.t, "src_ip": s.src_ip, "dst_ip": s.dst_ip,
                 "path": s.path, "G": s.G, "rerouted": s.rerouted}
                for s in self.flow_samples
            ],
            "throughput_samples": [
                {"t": s.t, "flow_id": s.flow_id, "mbps": s.mbps}
                for s in self.throughput_samples
            ],
            "reroute_events": [
                {
                    "t": e.t, "wall_time": e.wall_time,
                    "flow_id": e.flow_id, "src_ip": e.src_ip, "dst_ip": e.dst_ip,
                    "old_path": e.old_path, "new_path": e.new_path,
                    "trigger_reason": e.trigger_reason,
                    "bottleneck_util_before": e.bottleneck_util_before,
                    "bottleneck_util_after": e.bottleneck_util_after,
                    "alt_path_best_util": e.alt_path_best_util,
                    "improvement": e.improvement, "success": e.success,
                    "approach_specific": e.approach_specific,
                }
                for e in self.reroute_events
            ],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrialSamples":
        return cls(
            controller=d["controller"],
            approach=d.get("approach", d["controller"]),
            scenario=d["scenario"],
            trial=d["trial"],
            started_at=d["started_at"],
            finished_at=d["finished_at"],
            flow_ids=d.get("flow_ids", {}),
            notes=d.get("notes", ""),
            flow_start_results=d.get("flow_start_results", []),
            link_samples=[
                LinkSample(
                    t=s["t"], src=s["src"], dst=s["dst"], util=s["util"],
                    rate_mbps=s.get("rate_mbps", 0.0),
                    capacity_mbps=s.get("capacity_mbps", 0.0),
                    loss_fraction=s.get("loss_fraction", 0.0),
                ) for s in d.get("link_samples", [])
            ],
            flow_samples=[
                FlowSample(
                    t=s["t"], src_ip=s["src_ip"], dst_ip=s["dst_ip"],
                    path=s.get("path", []), G=s.get("G", 0.0),
                    rerouted=s.get("rerouted", False),
                ) for s in d.get("flow_samples", [])
            ],
            throughput_samples=[
                ThroughputSample(
                    t=s["t"], flow_id=s["flow_id"], mbps=s["mbps"]
                ) for s in d.get("throughput_samples", [])
            ],
            reroute_events=[
                RerouteEvent(
                    t=e["t"], wall_time=e.get("wall_time", e["t"]),
                    flow_id=e["flow_id"], src_ip=e["src_ip"], dst_ip=e["dst_ip"],
                    old_path=e.get("old_path", []),
                    new_path=e.get("new_path", []),
                    trigger_reason=e["trigger_reason"],
                    bottleneck_util_before=e.get("bottleneck_util_before"),
                    bottleneck_util_after=e.get("bottleneck_util_after"),
                    alt_path_best_util=e.get("alt_path_best_util"),
                    improvement=e.get("improvement"),
                    success=e.get("success", True),
                    approach_specific=e.get("approach_specific", {}),
                ) for e in d.get("reroute_events", [])
            ],
        )


# ── TrialRecorder ───────────────────────────────────────────────────────────


class TrialRecorder:
    """
    Execute one scenario trial and collect all samples.

    Lifecycle
    ---------
        rec = TrialRecorder(
            controller="active_inference",
            scenario=...,
            trial=0,
            rpc_client=rpc,
            state_path="state.json",
            event_log_path="/tmp/trial_0_events.jsonl",
        )
        samples = rec.run()      # blocks until scenario finishes
    """

    def __init__(
        self,
        controller: str,
        scenario: Scenario,
        trial: int,
        rpc_client: Optional[FlowRPCClient] = None,
        state_path: Optional[str] = None,
        event_log_path: Optional[str] = None,
    ) -> None:
        self.controller = controller
        self.scenario = scenario
        self.trial = trial
        self._rpc = rpc_client
        self._owns_rpc = rpc_client is None
        self._state_path = state_path
        # Event log path — if not provided, use a default in the cwd.
        # The auto_runner sets this per-trial so events are isolated.
        self._event_log_path = event_log_path or os.path.abspath(
            f"events_{controller}_{scenario.name}_trial{trial:02d}.jsonl"
        )
        self._poller: Optional[StatePoller] = None
        self._samples = TrialSamples(
            controller=controller,
            approach=controller,  # alias for plot clarity
            scenario=scenario.name,
            trial=trial,
            started_at=time.time(),
            finished_at=0.0,
        )
        self._t0 = self._samples.started_at

    def run(self) -> TrialSamples:
        """Execute the trial. Blocks for ~scenario.monitor_duration seconds."""
        # The auto_runner sets SDN_TRIAL_META_PATH in the MAIN process's
        # os.environ (in __init__), and the same value is propagated to
        # the controller subprocess's env in _start_controller. The
        # TrialEnv below writes the per-trial metadata to this file so
        # the controller picks it up on every log_reroute_event() call.
        #
        # If SDN_TRIAL_META_PATH is unset here, it means TrialRecorder
        # is being used outside the auto_runner (e.g. in manual mode or
        # in a smoke test). In that case TrialEnv falls back to a path
        # next to the event log file — which still works, as long as
        # the controller subprocess ALSO doesn't have SDN_TRIAL_META_PATH
        # set (so it falls back to the same default). The mismatch only
        # happens when ONE side sets it and the OTHER doesn't.
        trial_meta_path = os.environ.get("SDN_TRIAL_META_PATH")
        if trial_meta_path is None:
            print(
                f"  [WARN] SDN_TRIAL_META_PATH not set in main process env — "
                f"falling back to default next to event log. This is OK for "
                f"manual mode, but if you're running via auto_runner, this "
                f"means the controller subprocess and the recorder disagree "
                f"on where to write/read trial metadata → reroute events "
                f"will be silently lost.",
                flush=True,
            )
        else:
            print(
                f"  Trial metadata path: {trial_meta_path} "
                f"(controller subprocess reads same path via SDN_TRIAL_META_PATH)",
                flush=True,
            )
        env_ctx = sdn_event_log.TrialEnv(
            approach=self.controller,
            scenario=self.scenario.name,
            trial=self.trial,
            event_log_path=self._event_log_path,
            trial_meta_path=trial_meta_path,
        )
        # Note: TrialEnv truncates the event log file + writes trial metadata.
        env_ctx.__enter__()
        try:
            # Open RPC client if not provided.
            if self._rpc is None:
                self._rpc = FlowRPCClient()
                self._rpc.connect()
            try:
                # Make sure no leftover flows are running from a previous trial.
                try:
                    self._rpc.stop_all()
                except Exception as exc:
                    _log.warning("stop_all at trial start failed: %s", exc)

                # Start the state poller.
                self._poller = StatePoller(
                    path=self._state_path or "state.json"
                )
                self._poller.start()

                # Start the event drainer thread.
                drainer_stop = threading.Event()
                drainer = threading.Thread(
                    target=self._drain_events,
                    args=(drainer_stop,),
                    name="TrialRecorder-drain",
                    daemon=True,
                )
                drainer.start()

                # Warmup: trigger host learning by sending a quick ping
                # between each (src, dst) pair in the scenario. Without
                # this, the first iperf3 packet triggers host learning
                # AND flow installation simultaneously — if the controller
                # is slow (e.g. just started), iperf3 may time out before
                # flows are installed, resulting in zero throughput samples.
                # The ping is a small UDP flow (1 Mbps, 1s) that forces
                # the controller to learn both hosts and install flows
                # before the real iperf3 flows start.
                self._warmup_host_learning()

                # Schedule flows.
                self._schedule_flows()

                # Wait the rest of monitor_duration.
                end_time = self._t0 + self.scenario.monitor_duration
                while time.time() < end_time:
                    time.sleep(min(0.5, end_time - time.time()))

                # Final event drain.
                time.sleep(0.5)
                drainer_stop.set()
                drainer.join(timeout=2.0)
            finally:
                if self._poller is not None:
                    self._poller.stop()
                    self._collect_samples_from_poller()
                if self._owns_rpc and self._rpc is not None:
                    try:
                        self._rpc.stop_all()
                    except Exception:
                        pass
                    self._rpc.close()
        finally:
            env_ctx.__exit__(None, None, None)

        # Read structured reroute events from the JSONL log file.
        # This is the SINGLE source of truth — never inferred from
        # state.json path diffs.
        self._read_reroute_events()

        # ── Post-trial sanity check on the trial metadata file ──────────
        # If the controller subprocess never picked up the metadata
        # (because SDN_TRIAL_META_PATH was unset or pointed to a
        # different file), the per-trial event log file will be empty
        # and the controller will have written to a stray
        # reroute_events.jsonl in its cwd instead. Catch this class of
        # bug immediately rather than silently producing empty plots.
        self._check_trial_metadata_consistency()

        self._samples.finished_at = time.time()
        return self._samples

    def _check_trial_metadata_consistency(self) -> None:
        """
        Sanity-check that the trial metadata file actually exists and was
        written recently (i.e. by THIS trial's TrialEnv.__enter__).

        If the file is missing or stale, the controller subprocess
        couldn't have read the correct per-trial event log path from it
        — which means any reroute events it logged went to a stray file
        the recorder doesn't read back. This is the silent-failure mode
        that previously produced zero reroute events in every plot.

        Warns loudly (doesn't raise) so the user can spot the problem
        in the trial output instead of finding it post-hoc in empty
        plots.
        """
        trial_meta_path = os.environ.get("SDN_TRIAL_META_PATH")
        if trial_meta_path is None:
            # Manual mode / smoke test — TrialEnv used a default path
            # next to the event log. That's fine as long as the controller
            # ALSO doesn't have SDN_TRIAL_META_PATH set (which would be
            # the bug we're guarding against).
            return
        if not os.path.isfile(trial_meta_path):
            print(
                f"  [WARN] Trial metadata file missing: {trial_meta_path}\n"
                f"    The controller subprocess couldn't have read the\n"
                f"    per-trial event log path → reroute events were likely\n"
                f"    written to a stray file the recorder doesn't read.\n"
                f"    This trial's reroute_events will be empty.",
                flush=True,
            )
            return
        # Check the file was written recently (within the trial duration).
        try:
            mtime = os.path.getmtime(trial_meta_path)
            age_s = time.time() - mtime
            if age_s > self.scenario.monitor_duration + 60:
                print(
                    f"  [WARN] Trial metadata file is stale (age={age_s:.0f}s,\n"
                    f"    trial duration={self.scenario.monitor_duration:.0f}s):\n"
                    f"    {trial_meta_path}\n"
                    f"    The controller subprocess may have read an old\n"
                    f"    metadata file → reroute events may have been\n"
                    f"    written to a previous trial's log file.",
                    flush=True,
                )
        except OSError as exc:
            print(
                f"  [WARN] Could not stat trial metadata file {trial_meta_path}: {exc}",
                flush=True,
            )

    def _warmup_host_learning(self) -> None:
        """
        Send a quick 1-second UDP ping between each unique (src, dst) pair
        in the scenario to trigger host learning + flow installation in the
        controller BEFORE the real iperf3 flows start.

        This is critical when the controller was just started (e.g. the
        second controller after a controller switch) — without warmup, the
        first iperf3 packet triggers host learning, but the controller may
        not install flows fast enough, causing iperf3 to time out and
        produce zero throughput samples.

        The warmup flow is a 1-second UDP flow at 1 Mbps — just enough
        to force ARP resolution, host learning, and flow installation
        without affecting the network state meaningfully.

        Failures are recorded in flow_start_results (with the warmup
        flow_id prefixed by `_warmup_`) so they're visible in the trial
        JSON. They are NOT counted as benchmark flow failures (warmup
        is infrastructure, not measurement).
        """
        if not self._rpc:
            return
        # Collect unique (src, dst) pairs from the scenario steps.
        pairs = set()
        for step in self.scenario.steps:
            pairs.add((step.src, step.dst))
        if not pairs:
            return
        _log.debug("warmup: pinging %d host pairs", len(pairs))
        for src, dst in sorted(pairs):
            warmup_id = f"_warmup_{src}_{dst}"
            try:
                resp = self._rpc.start_flow({
                    "id": warmup_id,
                    "src": src,
                    "dst": dst,
                    "proto": "udp",
                    "bandwidth_mbps": 1.0,
                    "duration_s": 1,
                })
                ok = bool(resp.get("ok"))
                error = resp.get("error") if not ok else None
                # Record warmup result so failures are visible in the
                # trial JSON. The _warmup_ prefix lets the recorder/
                # sanity-check distinguish these from benchmark flows.
                self._samples.flow_start_results.append({
                    "flow_id": warmup_id,
                    "src": src,
                    "dst": dst,
                    "ok": ok,
                    "error": error,
                    "started_at": time.time(),
                    "warmup": True,
                })
                if not ok:
                    print(
                        f"  [WARMUP FAILED] scenario={self.scenario.name} "
                        f"approach={self.controller} trial={self.trial} "
                        f"flow_id={warmup_id} src={src} dst={dst} "
                        f"error={error}",
                        flush=True,
                    )
                else:
                    _log.debug("warmup %s->%s started", src, dst)
            except Exception as exc:
                self._samples.flow_start_results.append({
                    "flow_id": warmup_id,
                    "src": src,
                    "dst": dst,
                    "ok": False,
                    "error": f"exception: {exc}",
                    "started_at": time.time(),
                    "warmup": True,
                })
                print(
                    f"  [WARMUP EXCEPTION] scenario={self.scenario.name} "
                    f"approach={self.controller} trial={self.trial} "
                    f"flow_id={warmup_id} error={exc}",
                    flush=True,
                )
        # Wait for the warmup flows to complete (1s duration + 0.5s margin).
        time.sleep(1.5)
        # Stop any warmup flows that are still running. With the
        # FlowController fix (finished flows are deleted from self._flows),
        # this also frees the warmup IDs for reuse in the next trial.
        try:
            self._rpc.stop_all()
        except Exception:
            pass
        _log.debug("warmup complete")

    # ── Internals ─────────────────────────────────────────────────────

    def _schedule_flows(self) -> None:
        """Send start_flow/stop_flow commands at the scenario's offsets.

        Records every start_flow result (success or failure) in
        self._samples.flow_start_results so the trial JSON preserves
        the failure reason instead of silently producing empty samples.
        """
        if not self._rpc:
            return
        steps = sorted(self.scenario.steps, key=lambda s: s.start_offset)
        for step in steps:
            target = self._t0 + step.start_offset
            now = time.time()
            if target > now:
                time.sleep(target - now)
            try:
                resp = self._rpc.start_flow(step.to_rpc_params())
                ok = bool(resp.get("ok"))
                error = resp.get("error") if not ok else None
                # Record the result for diagnostics.
                self._samples.flow_start_results.append({
                    "flow_id": step.flow_id,
                    "src": step.src,
                    "dst": step.dst,
                    "ok": ok,
                    "error": error,
                    "started_at": time.time(),
                })
                if not ok:
                    # Don't silently continue — log loudly so the user
                    # sees the failure in real time.
                    print(
                        f"  [FLOW START FAILED] scenario={self.scenario.name} "
                        f"approach={self.controller} trial={self.trial} "
                        f"flow_id={step.flow_id} src={step.src} dst={step.dst} "
                        f"error={error}",
                        flush=True,
                    )
                    continue
                self._samples.flow_ids[step.flow_id] = {
                    "src": step.src,
                    "dst": step.dst,
                    "proto": step.proto,
                    "bandwidth_mbps": step.bandwidth_mbps,
                    "duration_s": step.duration_s,
                    "started_at": time.time(),
                }
                print(
                    f"  [FLOW START] scenario={self.scenario.name} "
                    f"approach={self.controller} trial={self.trial} "
                    f"flow_id={step.flow_id} src={step.src} dst={step.dst}",
                    flush=True,
                )
            except Exception as exc:
                # Network/protocol error — record it.
                self._samples.flow_start_results.append({
                    "flow_id": step.flow_id,
                    "src": step.src,
                    "dst": step.dst,
                    "ok": False,
                    "error": f"exception: {exc}",
                    "started_at": time.time(),
                })
                print(
                    f"  [FLOW START EXCEPTION] scenario={self.scenario.name} "
                    f"approach={self.controller} trial={self.trial} "
                    f"flow_id={step.flow_id} error={exc}",
                    flush=True,
                )
                _log.warning("start_flow %s raised: %s", step.flow_id, exc)

    def _drain_events(self, stop: threading.Event) -> None:
        """
        Background thread that drains RPC events into samples.

        IMPORTANT: warmup flow IDs (those starting with `_warmup_`) are
        EXCLUDED from throughput_samples. They're infrastructure traffic
        used to trigger host learning, not benchmark measurements.
        Including them would:
          - pollute per-flow throughput plots with warmup data
          - skew Jain's fairness (warmup is 1 Mbps vs real flows at 3+ Mbps)
          - make the flow-ID sanity check fail (AI would have an extra
            `_warmup_h1_h2` ID that Reactive wouldn't, since Reactive's
            warmup might fail for unrelated reasons)
        """
        if not self._rpc:
            return
        flow_log_count = 0
        flow_log_lines: List[str] = []  # keep last few for diagnostics
        while not stop.is_set():
            events = self._rpc.drain_events(timeout=0.5)
            for ev in events:
                if ev.get("type") != "event":
                    continue
                flow_id = ev.get("id", "?")
                is_warmup = flow_id.startswith("_warmup_")
                if ev.get("event") == "flow_stats":
                    if is_warmup:
                        # Warmup traffic — don't record as a benchmark
                        # throughput sample.
                        continue
                    self._samples.throughput_samples.append(
                        ThroughputSample(
                            t=time.time() - self._t0,
                            flow_id=flow_id,
                            mbps=float(ev.get("mbps", 0.0)),
                        )
                    )
                elif ev.get("event") == "flow_log":
                    if is_warmup:
                        # Don't count warmup logs in diagnostics either.
                        continue
                    flow_log_count += 1
                    line = ev.get("line", "")
                    if line:
                        flow_log_lines.append(f"[{flow_id}] {line}")
                        if len(flow_log_lines) > 20:
                            flow_log_lines.pop(0)
        # Store diagnostic info for the trial output.
        self._samples.notes = (
            f"flow_log_events={flow_log_count}"
            + (f"; last_lines={flow_log_lines[-3:]}" if flow_log_lines else "")
        )

    def _collect_samples_from_poller(self) -> None:
        """Pull state.json samples and convert to LinkSample / FlowSample."""
        if self._poller is None:
            return
        raw_samples = self._poller.samples()
        if not raw_samples:
            return
        t0 = raw_samples[0].get("_bench_wall_time", self._t0)

        for raw in raw_samples:
            t = raw.get("_bench_wall_time", t0) - t0
            for link in raw.get("links", []):
                if link.get("host_link"):
                    continue
                self._samples.link_samples.append(
                    LinkSample(
                        t=t,
                        src=str(link.get("src", "")),
                        dst=str(link.get("dst", "")),
                        util=float(link.get("util", 0.0)),
                        rate_mbps=float(link.get("rate_mbps", 0.0)),
                        capacity_mbps=float(link.get("capacity_mbps", 0.0)),
                        loss_fraction=float(link.get("loss_fraction", 0.0)),
                    )
                )
            for flow in raw.get("flows", []):
                src_ip = str(flow.get("src_ip", ""))
                dst_ip = str(flow.get("dst_ip", ""))
                path = list(flow.get("path", []))
                G = float(flow.get("G", 0.0))
                rerouted = bool(flow.get("rerouted", False))
                self._samples.flow_samples.append(
                    FlowSample(
                        t=t, src_ip=src_ip, dst_ip=dst_ip, path=path,
                        G=G, rerouted=rerouted,
                    )
                )

    def _read_reroute_events(self) -> None:
        """Read the structured reroute events JSONL file."""
        events_raw = sdn_event_log.read_events(self._event_log_path)
        for e in events_raw:
            # Convert wall_time to trial-relative time.
            wall_t = float(e.get("timestamp", 0.0))
            t_rel = wall_t - self._t0
            self._samples.reroute_events.append(
                RerouteEvent(
                    t=t_rel,
                    wall_time=wall_t,
                    flow_id=str(e.get("flow_id", "")),
                    src_ip=str(e.get("src_ip", "")),
                    dst_ip=str(e.get("dst_ip", "")),
                    old_path=list(e.get("old_path", [])),
                    new_path=list(e.get("new_path", [])),
                    trigger_reason=str(e.get("trigger_reason", "unknown")),
                    bottleneck_util_before=e.get("bottleneck_util_before"),
                    bottleneck_util_after=e.get("bottleneck_util_after"),
                    alt_path_best_util=e.get("alt_path_best_util"),
                    improvement=e.get("improvement"),
                    success=bool(e.get("success", True)),
                    approach_specific=e.get("approach_specific", {}),
                )
            )
        # Sort by trial-relative time.
        self._samples.reroute_events.sort(key=lambda ev: ev.t)


# ── Persistence ─────────────────────────────────────────────────────────────


def save_trial(samples: TrialSamples, dir_path: str) -> str:
    """Save a trial's samples to a JSON file. Returns the file path."""
    os.makedirs(dir_path, exist_ok=True)
    fname = f"trial{samples.trial:02d}.json"
    path = os.path.join(dir_path, fname)
    with open(path, "w") as fh:
        json.dump(samples.to_dict(), fh, indent=2)
    return path


def load_trial(path: str) -> TrialSamples:
    with open(path, "r") as fh:
        return TrialSamples.from_dict(json.load(fh))


def load_all_trials(dir_path: str) -> List[TrialSamples]:
    """Load all trial JSON files from a directory (recursive)."""
    out: List[TrialSamples] = []
    if not os.path.isdir(dir_path):
        return out
    for root, _dirs, files in os.walk(dir_path):
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            try:
                out.append(load_trial(os.path.join(root, fname)))
            except Exception as exc:
                _log.warning("load_all_trials: skip %s: %s", fname, exc)
    return out
