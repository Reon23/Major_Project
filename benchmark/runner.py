"""
benchmark/runner.py — Top-level orchestrator.

Run N trials per (controller, scenario) pair, save each trial's raw
samples to disk, then call the plotter to generate comparison plots.

This is the main "loop" the user drives from the CLI:
  for each controller:
    wait for the user to start that controller + network
    wait for state.json + FlowRPCServer to be reachable
    for each scenario:
      for trial in range(N):
        run TrialRecorder
        save trial samples
  generate comparison plots
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

from benchmark.metrics import aggregate_metrics
from benchmark.plotter import generate_comparison_plots
from benchmark.recorder import (
    TrialRecorder,
    TrialSamples,
    load_all_trials,
    save_trial,
)
from benchmark.rpc_client import FlowRPCClient, server_reachable
from benchmark.scenarios import SCENARIOS, Scenario, get_scenario
from benchmark.state_poller import read_state, state_mtime

_log = logging.getLogger(__name__)


CONTROLLERS = ("active_inference", "reactive")
DEFAULT_SCENARIO_NAMES = [s.name for s in SCENARIOS]


class BenchmarkRunner:
    """
    Orchestrates the full benchmark run.

    Usage
    -----
        runner = BenchmarkRunner(trials=3, output_dir="bench_results")
        runner.run_interactive(
            controllers=["active_inference", "reactive"],
            scenarios=["baseline", "forced_bottleneck", ...],
        )
        # plots + summary are in bench_results/plots/
    """

    def __init__(
        self,
        trials: int = 3,
        output_dir: str = "bench_results",
        trials_dir: Optional[str] = None,
        plots_dir: Optional[str] = None,
        state_path: Optional[str] = None,
        rpc_host: Optional[str] = None,
        rpc_port: Optional[int] = None,
    ) -> None:
        self.trials = max(1, int(trials))
        self.output_dir = output_dir
        self.trials_dir = trials_dir or os.path.join(output_dir, "trials")
        self.plots_dir = plots_dir or os.path.join(output_dir, "plots")
        self.state_path = state_path
        self.rpc_host = rpc_host
        self.rpc_port = rpc_port
        os.makedirs(self.trials_dir, exist_ok=True)
        os.makedirs(self.plots_dir, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────

    def run_interactive(
        self,
        controllers: Optional[List[str]] = None,
        scenarios: Optional[List[str]] = None,
        confirm_each_controller: bool = True,
    ) -> Dict[str, Dict[str, List[TrialSamples]]]:
        """
        Run the full benchmark.

        For each controller, prompts the user to start that controller
        (interactive pause — user starts ryu-manager + mininet themselves
        in another terminal), then waits for state.json + FlowRPCServer
        to be reachable, then runs N trials per scenario.

        Returns the collected trials organized as
        {controller: {scenario: [TrialSamples, ...]}}.
        """
        controllers = controllers or list(CONTROLLERS)
        scenarios = scenarios or DEFAULT_SCENARIO_NAMES
        all_trials: Dict[str, Dict[str, List[TrialSamples]]] = {
            c: {} for c in controllers
        }

        for controller in controllers:
            if controller not in CONTROLLERS:
                _log.warning(
                    "Unknown controller %r — skipping. Known: %s",
                    controller, CONTROLLERS,
                )
                continue
            print()
            print("=" * 72)
            print(f"  Controller: {controller}")
            print("=" * 72)

            if confirm_each_controller:
                self._prompt_controller_start(controller)

            # Wait for state.json + RPC server to be reachable.
            self._wait_for_infrastructure(controller)

            # Open one persistent RPC client for all trials of this controller.
            rpc = FlowRPCClient(
                host=self.rpc_host or "127.0.0.1",
                port=self.rpc_port or 5566,
            )
            try:
                rpc.connect()
            except Exception as exc:
                print(f"  [ERROR] Could not connect to FlowRPCServer: {exc}")
                print("  Skipping this controller.")
                continue

            try:
                for scenario_name in scenarios:
                    scenario = get_scenario(scenario_name)
                    if scenario is None:
                        _log.warning(
                            "Unknown scenario %r — skipping", scenario_name
                        )
                        continue
                    print()
                    print(f"  Scenario: {scenario_name} "
                          f"({len(scenario.steps)} flows, "
                          f"~{scenario.monitor_duration:.0f}s/trial)")
                    trial_list: List[TrialSamples] = []
                    for trial_idx in range(self.trials):
                        print(f"    trial {trial_idx + 1}/{self.trials}... ",
                              end="", flush=True)
                        try:
                            rec = TrialRecorder(
                                controller=controller,
                                scenario=scenario,
                                trial=trial_idx,
                                rpc_client=rpc,
                                state_path=self.state_path,
                            )
                            samples = rec.run()
                            save_trial(samples, self.trials_dir)
                            trial_list.append(samples)
                            print(
                                f"done "
                                f"(links={len(samples.link_samples)}, "
                                f"flows={len(samples.flow_samples)}, "
                                f"throughput={len(samples.throughput_samples)}, "
                                f"reroutes={len(samples.reroute_events)})"
                            )
                        except KeyboardInterrupt:
                            print("\n  [interrupted]")
                            raise
                        except Exception as exc:
                            print(f"FAILED ({exc})")
                            _log.exception("trial failed")
                    all_trials[controller][scenario_name] = trial_list
            finally:
                rpc.close()

        # Generate comparison plots.
        print()
        print("=" * 72)
        print("  Generating comparison plots...")
        print("=" * 72)
        files = generate_comparison_plots(all_trials, self.plots_dir)
        for name, path in files.items():
            print(f"    {name}: {path}")
        print()
        print("  Done.")
        return all_trials

    # ── Re-run plots from saved trials ────────────────────────────────

    def regenerate_plots(
        self,
        controllers: Optional[List[str]] = None,
        scenarios: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """Re-aggregate from saved trial JSONs and regenerate plots.

        Useful when you want to add more trials or tweak plot styling
        without re-running everything.
        """
        all_trials = load_all_trials(self.trials_dir)
        # Group by controller + scenario.
        grouped: Dict[str, Dict[str, List[TrialSamples]]] = {}
        for t in all_trials:
            if controllers and t.controller not in controllers:
                continue
            if scenarios and t.scenario not in scenarios:
                continue
            grouped.setdefault(t.controller, {}).setdefault(t.scenario, []).append(t)
        return generate_comparison_plots(grouped, self.plots_dir)

    # ── Internals ─────────────────────────────────────────────────────

    def _prompt_controller_start(self, controller: str) -> None:
        """Print instructions and wait for the user to hit Enter."""
        ryu_file = (
            "active_inference_dynamic.py"
            if controller == "active_inference"
            else "reactive_dynamic.py"
        )
        print()
        print("  Please start this controller + network in another terminal:")
        print()
        print(f"    Terminal 1 — controller:")
        print(f"      ryu-manager --observe-links {ryu_file}")
        print()
        print(f"    Terminal 2 — mininet (needs root):")
        print(f"      sudo mn -c && sudo python3 topology.py")
        print()
        print("  (or use `python3 app.py` / `python3 app_reactive.py`")
        print("   and click 'Start Everything' in the GUI)")
        print()
        print("  Wait until the controller logs show switches discovered")
        print("  (e.g. 'Switch s1 connected' ... 'Switch s8 connected'),")
        print("  then return here.")
        print()
        try:
            input("  Press ENTER when ready, or Ctrl-C to abort... ")
        except KeyboardInterrupt:
            raise SystemExit(0)

    def _wait_for_infrastructure(
        self, controller: str, timeout: float = 120.0
    ) -> None:
        """Block until state.json exists + FlowRPCServer is reachable."""
        deadline = time.time() + timeout
        state_ok = False
        rpc_ok = False
        print(f"  Waiting for state.json + FlowRPCServer (up to {timeout:.0f}s)...",
              end="", flush=True)
        last_msg_t = time.time()
        while time.time() < deadline:
            if not state_ok:
                st = read_state(self.state_path or "state.json")
                if st is not None and st.get("nodes"):
                    state_ok = True
                    print(" state.json✓", end="", flush=True)
                    last_msg_t = time.time()
            if not rpc_ok:
                if server_reachable(
                    self.rpc_host or "127.0.0.1",
                    self.rpc_port or 5566,
                ):
                    rpc_ok = True
                    print(" rpc✓", end="", flush=True)
                    last_msg_t = time.time()
            if state_ok and rpc_ok:
                print()
                return
            # Heartbeat so the user knows we're alive.
            if time.time() - last_msg_t > 15:
                print(".", end="", flush=True)
                last_msg_t = time.time()
            time.sleep(0.5)
        print()
        raise RuntimeError(
            f"Timed out after {timeout}s waiting for infrastructure "
            f"(state_ok={state_ok}, rpc_ok={rpc_ok}). "
            f"Make sure the controller and topology.py are both running."
        )


# ── Convenience: list saved trials ──────────────────────────────────────────


def list_saved_trials(trials_dir: str = "bench_results/trials") -> None:
    """Print a summary of all saved trial files."""
    trials = load_all_trials(trials_dir)
    if not trials:
        print(f"No trials found in {trials_dir}/")
        return
    # Group by (controller, scenario).
    by_group: Dict[tuple, List[TrialSamples]] = {}
    for t in trials:
        by_group.setdefault((t.controller, t.scenario), []).append(t)
    print(f"Trials in {trials_dir}/")
    print("=" * 72)
    for (controller, scenario), trial_list in sorted(by_group.items()):
        # For each, print a summary line.
        n_links = sum(len(t.link_samples) for t in trial_list)
        n_flows = sum(len(t.flow_samples) for t in trial_list)
        n_tp = sum(len(t.throughput_samples) for t in trial_list)
        n_re = sum(len(t.reroute_events) for t in trial_list)
        print(
            f"  {controller:18s} | {scenario:20s} | "
            f"{len(trial_list)} trials | "
            f"links={n_links:5d} flows={n_flows:5d} "
            f"throughput={n_tp:5d} reroutes={n_re}"
        )
