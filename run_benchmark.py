#!/usr/bin/env python3
"""
run_benchmark.py — CLI entry point for the SDN benchmark harness.

Default behaviour: `run` starts everything itself (ryu-manager + sudo
topology.py), runs N trials per (controller, scenario), switches
controllers mid-run, generates plots. No need for three terminals.

Subcommands:
  run       One-command benchmark (default — auto-starts everything).
  compare   Re-aggregate saved trials and regenerate plots + summary.
  list      List all saved trials.
  scenarios Print the predefined scenarios.

Examples
--------
  # Full benchmark — single command, starts ryu + mininet itself
  python3 run_benchmark.py run --trials 3

  # Quick smoke test: 1 trial, 2 scenarios
  python3 run_benchmark.py run --trials 1 --scenarios baseline,forced_bottleneck

  # Run only one controller (e.g. for an incremental run)
  python3 run_benchmark.py run --trials 3 --controllers active_inference

  # Manual mode: you've already started ryu + mininet in another
  # terminal, the harness just connects and runs trials (the old
  # behaviour — useful for debugging one layer in isolation).
  python3 run_benchmark.py run --manual --trials 3

  # Pass sudo password via env var (no prompt)
  SDN_SUDO_PASSWORD=mypass python3 run_benchmark.py run --trials 3

  # Regenerate plots from saved trials (no network needed)
  python3 run_benchmark.py compare

  # See what's been saved
  python3 run_benchmark.py list
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from benchmark.runner import (
    BenchmarkRunner,
    DEFAULT_SCENARIO_NAMES,
    CONTROLLERS,
    list_saved_trials,
)
from benchmark.scenarios import SCENARIOS


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_run(args: argparse.Namespace) -> int:
    controllers = (
        args.controllers.split(",") if args.controllers else list(CONTROLLERS)
    )
    scenarios = (
        args.scenarios.split(",") if args.scenarios else list(DEFAULT_SCENARIO_NAMES)
    )

    if args.manual:
        # Old interactive mode — user starts ryu + mininet themselves.
        runner = BenchmarkRunner(
            trials=args.trials,
            output_dir=args.output_dir,
            trials_dir=args.trials_dir,
            plots_dir=args.plots_dir,
            state_path=args.state_path,
            rpc_host=args.rpc_host,
            rpc_port=args.rpc_port,
        )
        runner.run_interactive(
            controllers=controllers,
            scenarios=scenarios,
            confirm_each_controller=not args.no_prompt,
        )
        return 0

    # Default: auto mode — start everything ourselves.
    from benchmark.auto_runner import AutoBenchmarkRunner
    runner = AutoBenchmarkRunner(
        trials=args.trials,
        output_dir=args.output_dir,
        trials_dir=args.trials_dir,
        plots_dir=args.plots_dir,
        spec_path=args.spec,
        python_bin=args.python_bin,
        ryu_manager_bin=args.ryu_manager,
        sudo_password=args.sudo_password or os.environ.get("SDN_SUDO_PASSWORD"),
        state_path=args.state_path,
    )
    runner.run(controllers=controllers, scenarios=scenarios)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    runner = BenchmarkRunner(
        trials=0,  # not used for re-aggregation
        output_dir=args.output_dir,
        trials_dir=args.trials_dir,
        plots_dir=args.plots_dir,
    )
    controllers = (
        args.controllers.split(",") if args.controllers else None
    )
    scenarios = (
        args.scenarios.split(",") if args.scenarios else None
    )
    files = runner.regenerate_plots(controllers=controllers, scenarios=scenarios)
    print(f"\nGenerated {len(files)} files in {runner.plots_dir}/:")
    for name, path in files.items():
        print(f"  {name}: {path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    list_saved_trials(args.trials_dir or "bench_results/trials")
    return 0


def cmd_scenarios(args: argparse.Namespace) -> int:
    print("Predefined scenarios:")
    print("=" * 72)
    for s in SCENARIOS:
        print(f"\n  {s.name}  ({len(s.steps)} flows, ~{s.monitor_duration:.0f}s/trial)")
        print(f"  {s.description}")
        for step in s.steps:
            print(
                f"    t+{step.start_offset:5.1f}s  {step.flow_id:14s}  "
                f"{step.src}->{step.dst}  {step.proto} {step.bandwidth_mbps}Mbps "
                f"({step.duration_s}s)"
            )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_benchmark.py",
        description=(
            "SDN active-inference vs reactive controller benchmark harness.\n"
            "Default: 'run' starts ryu + mininet itself and runs the full "
            "comparison in one command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── run ────────────────────────────────────────────────────────────
    p_run = sub.add_parser(
        "run",
        help="Run N trials per (controller, scenario). Auto-starts everything.",
        description=cmd_run.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_run.add_argument("--trials", type=int, default=3,
                       help="number of trials per (controller, scenario)")
    p_run.add_argument("--controllers", type=str, default=None,
                       help=f"comma-separated controller names "
                            f"(default: {','.join(CONTROLLERS)})")
    p_run.add_argument("--scenarios", type=str, default=None,
                       help="comma-separated scenario names "
                            f"(default: {','.join(DEFAULT_SCENARIO_NAMES)})")
    p_run.add_argument("--output-dir", type=str, default="bench_results",
                       help="root output directory (default: bench_results)")
    p_run.add_argument("--trials-dir", type=str, default=None,
                       help="trial JSON output dir (default: <output-dir>/trials)")
    p_run.add_argument("--plots-dir", type=str, default=None,
                       help="plot output dir (default: <output-dir>/plots)")

    # Auto-mode-specific args.
    p_run.add_argument("--manual", action="store_true",
                       help="don't auto-start ryu/mininet — connect to an "
                            "already-running controller + network (the old "
                            "interactive behaviour; useful for debugging)")
    p_run.add_argument("--spec", type=str, default=None,
                       help="topology spec JSON path (default: topology_spec.json)")
    p_run.add_argument("--python-bin", type=str, default="python3",
                       help="python binary for spawning topology.py (default: python3)")
    p_run.add_argument("--ryu-manager", type=str, default="ryu-manager",
                       help="ryu-manager binary (default: ryu-manager)")
    p_run.add_argument("--sudo-password", type=str, default=None,
                       help="sudo password (alternative to env var "
                            "SDN_SUDO_PASSWORD or interactive prompt)")
    p_run.add_argument("--state-path", type=str, default=None,
                       help="state.json path (default: state.json in project dir)")
    p_run.add_argument("--rpc-host", type=str, default=None,
                       help="FlowRPCServer host (default: 127.0.0.1)")
    p_run.add_argument("--rpc-port", type=int, default=None,
                       help="FlowRPCServer port (default: 5566)")
    p_run.add_argument("--no-prompt", action="store_true",
                       help="(manual mode only) skip the controller-start "
                            "confirmation prompt")
    p_run.set_defaults(func=cmd_run)

    # ── compare ────────────────────────────────────────────────────────
    p_cmp = sub.add_parser(
        "compare",
        help="Re-aggregate saved trials and regenerate plots + summary.",
        description=cmd_compare.__doc__,
    )
    p_cmp.add_argument("--output-dir", type=str, default="bench_results")
    p_cmp.add_argument("--trials-dir", type=str, default=None)
    p_cmp.add_argument("--plots-dir", type=str, default=None)
    p_cmp.add_argument("--controllers", type=str, default=None,
                       help="filter to specific controllers")
    p_cmp.add_argument("--scenarios", type=str, default=None,
                       help="filter to specific scenarios")
    p_cmp.set_defaults(func=cmd_compare)

    # ── list ───────────────────────────────────────────────────────────
    p_list = sub.add_parser(
        "list",
        help="List saved trials.",
        description=cmd_list.__doc__,
    )
    p_list.add_argument("--trials-dir", type=str, default=None)
    p_list.set_defaults(func=cmd_list)

    # ── scenarios ──────────────────────────────────────────────────────
    p_scn = sub.add_parser(
        "scenarios",
        help="Print predefined scenario definitions.",
        description=cmd_scenarios.__doc__,
    )
    p_scn.set_defaults(func=cmd_scenarios)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
