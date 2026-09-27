"""
sdn/event_log.py — Shared structured reroute event logger.

Both the Active Inference controller (active_inference_dynamic.py) and
the Reactive controller (reactive_dynamic.py) call `log_reroute_event()`
whenever they make a routing decision. Events are appended to a JSONL
file (one event per line) at the path given by the `SDN_EVENT_LOG_PATH`
env var (default: `reroute_events.jsonl` in the cwd).

This is the SINGLE source of truth for routing decisions — the benchmark
recorder reads from this file, NOT from state.json path diffs (which
miss intermediate changes and have no metadata). State.json is still
written for the visualizer, but reroute events are now first-class
structured records.

Trial metadata propagation
--------------------------
The benchmark runner spawns ONE controller subprocess per controller
(Active Inference or Reactive), then runs N trials × M scenarios
against it. The per-trial metadata (scenario name, trial index,
event log path) cannot be passed via env vars at controller-spawn
time, because the controller is already running by the time the
recorder starts a new trial.

Instead, the benchmark runner writes a small "trial metadata" file
(`trial_metadata.json`) at a path the controller reads on EVERY event
log call. The path is given by SDN_TRIAL_META_PATH env var (set at
controller spawn time, points to a stable file location). The runner
updates this file before each trial; the controller picks up the new
values immediately.

Schema
------
Each event is a JSON object on its own line:

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

Trigger reasons (standardised across both controllers):
  cold_start                     — first path selection for a flow
  congestion_threshold_crossed   — Reactive: threshold crossed, better path found
  no_better_path                 — Reactive: threshold crossed but no path improves
                                    enough (failed/ineffective reroute attempt)
  efe_switch                     — AI: EFE analysis recommended switching paths
  efe_split                      — AI: EFE analysis recommended multipath split
  ecmp_fallback                  — Reactive: equal-weight ECMP fallback
  hysteresis_hold                — Reactive: cooldown prevented a reroute
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional


# ── Constants ───────────────────────────────────────────────────────────────


ENV_EVENT_LOG_PATH = "SDN_EVENT_LOG_PATH"
DEFAULT_EVENT_LOG_PATH = "reroute_events.jsonl"

ENV_TRIAL_META_PATH = "SDN_TRIAL_META_PATH"
DEFAULT_TRIAL_META_PATH = "trial_metadata.json"

ENV_APPROACH = "SDN_APPROACH"          # fallback if no metadata file


# ── Standard trigger reasons ──────────────────────────────────────────────


REASON_COLD_START = "cold_start"
REASON_CONGESTION = "congestion_threshold_crossed"
REASON_NO_BETTER_PATH = "no_better_path"
REASON_EFE_SWITCH = "efe_switch"
REASON_EFE_SPLIT = "efe_split"
REASON_ECMP_FALLBACK = "ecmp_fallback"
REASON_HYSTERESIS_HOLD = "hysteresis_hold"


# ── Logger ─────────────────────────────────────────────────────────────────


_lock = threading.Lock()
_metadata_cache: Optional[Dict[str, Any]] = None
_metadata_cache_mtime: Optional[float] = None


def _resolve_log_path(meta: Dict[str, Any]) -> str:
    """Resolve the event log path from trial metadata or env var."""
    if "event_log_path" in meta and meta["event_log_path"]:
        return meta["event_log_path"]
    return os.environ.get(ENV_EVENT_LOG_PATH, DEFAULT_EVENT_LOG_PATH)


def _resolve_trial_meta_path() -> str:
    return os.environ.get(ENV_TRIAL_META_PATH, DEFAULT_TRIAL_META_PATH)


def _read_trial_metadata() -> Dict[str, Any]:
    """
    Read the trial metadata file (path from SDN_TRIAL_META_PATH env var).

    The benchmark runner updates this file before each trial; the
    controller reads it on every event-log call. Cached by mtime so
    the file is only re-read when it changes.

    Falls back gracefully if the file doesn't exist or is unreadable —
    the event will still be logged, just with empty trial metadata.
    """
    global _metadata_cache, _metadata_cache_mtime
    path = _resolve_trial_meta_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        # File doesn't exist (yet) — use the cached version if any,
        # else fall back to env vars.
        if _metadata_cache is not None:
            return _metadata_cache
        return {
            "trial_id": "unknown",
            "approach": os.environ.get(ENV_APPROACH, "unknown"),
            "scenario": "unknown",
            "trial": -1,
            "event_log_path": os.environ.get(
                ENV_EVENT_LOG_PATH, DEFAULT_EVENT_LOG_PATH
            ),
        }
    # Cache hit.
    if _metadata_cache is not None and _metadata_cache_mtime == mtime:
        return _metadata_cache or {}
    # Re-read.
    try:
        with open(path, "r") as fh:
            meta = json.load(fh)
        _metadata_cache = meta
        _metadata_cache_mtime = mtime
        return meta
    except (json.JSONDecodeError, OSError):
        # Bad file — fall back to whatever we had.
        return _metadata_cache or {}


def log_reroute_event(
    flow_id: str,
    src_ip: str,
    dst_ip: str,
    old_path: Optional[List[Any]],
    new_path: List[Any],
    trigger_reason: str,
    bottleneck_util_before: Optional[float] = None,
    bottleneck_util_after: Optional[float] = None,
    alt_path_best_util: Optional[float] = None,
    improvement: Optional[float] = None,
    success: bool = True,
    approach_specific: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Append one structured reroute event to the JSONL log file.

    Trial metadata (approach, scenario, trial_idx, event_log_path) is
    read from the trial metadata file on every call — this lets the
    benchmark runner change the active trial without restarting the
    controller subprocess.

    Safe to call from either controller at any time. Thread-safe. If
    the log path is unwritable, the event is silently dropped (the
    controller must NEVER crash because of logging).
    """
    meta = _read_trial_metadata()
    event = {
        "timestamp": time.time(),
        "trial_id": str(meta.get("trial_id", "unknown")),
        "approach": str(meta.get("approach", "unknown")),
        "scenario": str(meta.get("scenario", "unknown")),
        "trial": int(meta.get("trial", -1)),
        "flow_id": str(flow_id),
        "src_ip": str(src_ip),
        "dst_ip": str(dst_ip),
        "old_path": [str(s) for s in old_path] if old_path else [],
        "new_path": [str(s) for s in new_path] if new_path else [],
        "trigger_reason": str(trigger_reason),
        "bottleneck_util_before": (
            round(float(bottleneck_util_before), 5)
            if bottleneck_util_before is not None
            else None
        ),
        "bottleneck_util_after": (
            round(float(bottleneck_util_after), 5)
            if bottleneck_util_after is not None
            else None
        ),
        "alt_path_best_util": (
            round(float(alt_path_best_util), 5)
            if alt_path_best_util is not None
            else None
        ),
        "improvement": (
            round(float(improvement), 5)
            if improvement is not None
            else None
        ),
        "success": bool(success),
        "approach_specific": dict(approach_specific) if approach_specific else {},
    }
    path = _resolve_log_path(meta)
    try:
        line = json.dumps(event, sort_keys=True) + "\n"
        with _lock:
            with open(path, "a") as fh:
                fh.write(line)
    except (OSError, TypeError) as exc:
        # Logging is best-effort. Don't crash the controller.
        import sys
        print(f"[sdn.event_log] failed to write event: {exc}", file=sys.stderr)
    return event


# ── Reader (used by the benchmark recorder) ───────────────────────────────


def read_events(path: str) -> List[Dict[str, Any]]:
    """Read all events from a JSONL log file."""
    if not os.path.isfile(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    import sys
                    print(
                        f"[sdn.event_log] bad JSON at {path}:{line_no}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                out.append(event)
    except OSError as exc:
        import sys
        print(f"[sdn.event_log] read failed for {path}: {exc}", file=sys.stderr)
    return out


def clear_log(path: Optional[str] = None) -> None:
    """Truncate the log file."""
    path = path or _resolve_log_path(_read_trial_metadata())
    try:
        with _lock:
            open(path, "w").close()
    except OSError:
        pass


# ── Trial metadata file writer (called by the benchmark runner) ────────────


def write_trial_metadata(
    path: str,
    approach: str,
    scenario: str,
    trial: int,
    event_log_path: str,
) -> None:
    """
    Write the trial metadata file. The controller reads this on every
    event-log call to know which trial is currently running.

    Called by benchmark/auto_runner.py before each trial, and by
    benchmark/recorder.py's TrialEnv context manager.
    """
    meta = {
        "trial_id": f"{approach}__{scenario}__trial{trial:02d}",
        "approach": approach,
        "scenario": scenario,
        "trial": trial,
        "event_log_path": event_log_path,
        "updated_at": time.time(),
    }
    try:
        # Atomic write via temp + rename.
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(meta, fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        import sys
        print(f"[sdn.event_log] failed to write trial metadata: {exc}",
              file=sys.stderr)
    # Invalidate our cache so the next read picks up the new values.
    global _metadata_cache, _metadata_cache_mtime
    _metadata_cache = None
    _metadata_cache_mtime = None


# ── Trial env-var context manager (used by the benchmark runner) ──────────


class TrialEnv:
    """
    Context manager that writes the trial metadata file + truncates the
    event log file before each trial. The controller (running as a
    subprocess inherited from auto_runner) reads the metadata file on
    every event-log call, so it picks up the new trial's metadata
    immediately.

    The metadata file path is given by `trial_meta_path` (passed in
    here, also set as SDN_TRIAL_META_PATH env var at controller-spawn
    time so the controller knows where to read from).
    """

    def __init__(
        self,
        approach: str,
        scenario: str,
        trial: int,
        event_log_path: str,
        trial_meta_path: Optional[str] = None,
    ) -> None:
        self._approach = approach
        self._scenario = scenario
        self._trial = trial
        self._event_log_path = event_log_path
        # Default metadata file path: same dir as the event log.
        if trial_meta_path is None:
            trial_meta_path = os.path.join(
                os.path.dirname(event_log_path),
                "trial_metadata.json",
            )
        self._trial_meta_path = trial_meta_path

    def __enter__(self) -> "TrialEnv":
        # Truncate the event log so this trial starts clean.
        clear_log(self._event_log_path)
        # Write the trial metadata file.
        write_trial_metadata(
            path=self._trial_meta_path,
            approach=self._approach,
            scenario=self._scenario,
            trial=self._trial,
            event_log_path=self._event_log_path,
        )
        return self

    def __exit__(self, *exc) -> None:
        # Nothing to clean up — the metadata file stays for the next
        # trial to overwrite.
        pass

    @property
    def trial_meta_path(self) -> str:
        return self._trial_meta_path


# ── Convenience: build a flow_id from src/dst IP ──────────────────────────


def make_flow_id(src_ip: str, dst_ip: str) -> str:
    """Standard flow_id: 'src_ip->dst_ip'."""
    return f"{src_ip}->{dst_ip}"
