"""
benchmark/state_poller.py — Atomic reader for the controller's state.json.

The controller's sdn/state_writer.py writes state.json via temp-file +
os.replace, so we always read either the previous complete file or
the new complete file — never a partial. We just need to be tolerant
of a brief "file is being replaced" window (try/except around the read).

Polls at POLL_INTERVAL (2 s, the same cadence the controller itself
uses) by default; the recorder can poll faster if needed for bursty
scenarios — but reading more often than the file is written just
returns the same snapshot, so there's no point.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from sdn.constants import POLL_INTERVAL, STATE_JSON_PATH

_log = logging.getLogger(__name__)


def read_state(path: str = STATE_JSON_PATH) -> Optional[Dict[str, Any]]:
    """
    Read state.json atomically. Returns None if the file doesn't exist
    or is momentarily unavailable (during os.replace). Caller should
    treat None as "no new sample this tick".
    """
    try:
        # os.replace is atomic on POSIX; the open() below may briefly
        # fail with FileNotFoundError if we race a replacement, so we
        # retry once after a tiny sleep.
        with open(path, "r") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        _log.debug("state_poller: read failed for %s: %s", path, exc)
        return None


def state_mtime(path: str = STATE_JSON_PATH) -> Optional[float]:
    """Return the file's mtime, or None if it doesn't exist."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


class StatePoller:
    """
    Background thread that polls state.json and pushes each new sample
    (by mtime change) to a queue.

    Used by TrialRecorder to drive sample collection.
    """

    def __init__(
        self,
        path: str = STATE_JSON_PATH,
        interval: float = POLL_INTERVAL,
    ) -> None:
        self._path = path
        self._interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._samples: list[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="StatePoller", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def samples(self) -> list[Dict[str, Any]]:
        """Return a copy of all collected samples so far."""
        with self._lock:
            return list(self._samples)

    def _loop(self) -> None:
        last_mtime: Optional[float] = None
        while not self._stop.is_set():
            mtime = state_mtime(self._path)
            if mtime is not None and mtime != last_mtime:
                last_mtime = mtime
                state = read_state(self._path)
                if state is not None:
                    with self._lock:
                        # Tag with our own wall-clock read time so the
                        # recorder has a consistent timestamp axis even
                        # if the controller's `timestamp` ISO field is
                        # missing or malformed.
                        state["_bench_wall_time"] = time.time()
                        self._samples.append(state)
            self._stop.wait(self._interval)
