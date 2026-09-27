"""
benchmark/ — Standalone benchmarking harness for the SDN active-inference
vs reactive controller comparison.

Reads only what the existing controllers already expose:
  * state.json (atomic JSON written by sdn/state_writer.py every POLL_INTERVAL)
  * FlowRPCServer on sdn.traffic_protocol.TRAFFIC_RPC_HOST:PORT (JSON-line TCP)

Does NOT modify the controllers or their state schema.

Modules
-------
  scenarios.py     — predefined traffic patterns (baseline / bottleneck /
                     ramp / bursty-flap)
  rpc_client.py    — JSON-line TCP client for FlowRPCServer
  state_poller.py  — atomic reader for state.json
  recorder.py      — per-trial sample collector
  metrics.py       — Jain's fairness, p95, CoV, time-to-recover, reroute count
  plotter.py       — matplotlib overlay plots + summary bar charts
  runner.py        — top-level orchestrator (scenario × N trials × controller)

Entry point
-----------
  run_benchmark.py — CLI: `run`, `compare`, `list`
"""

from benchmark.scenarios import SCENARIOS, Scenario, FlowStep  # noqa: F401
from benchmark.rpc_client import FlowRPCClient, FlowRPCError  # noqa: F401
from benchmark.state_poller import read_state, StatePoller  # noqa: F401
from benchmark.recorder import TrialRecorder, TrialSamples  # noqa: F401
from benchmark.metrics import (  # noqa: F401
    aggregate_metrics,
    jains_fairness,
    mean_link_util,
    max_link_util,
    p95_link_util,
    mean_packet_loss,
    mean_throughput,
    throughput_cov,
    total_reroute_count,
    successful_reroute_count,
    failed_reroute_count,
    time_to_first_reroute,
    time_to_recover,
    congestion_events_count,
)

__all__ = [
    "SCENARIOS",
    "Scenario",
    "FlowStep",
    "FlowRPCClient",
    "FlowRPCError",
    "read_state",
    "StatePoller",
    "TrialRecorder",
    "TrialSamples",
    "aggregate_metrics",
    "jains_fairness",
    "mean_link_util",
    "max_link_util",
    "p95_link_util",
    "mean_packet_loss",
    "mean_throughput",
    "throughput_cov",
    "total_reroute_count",
    "successful_reroute_count",
    "failed_reroute_count",
    "time_to_first_reroute",
    "time_to_recover",
    "congestion_events_count",
]
