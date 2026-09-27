"""
benchmark/auto_runner.py — One-command benchmark runner.

Mirrors process_manager.py's pattern (spawn `ryu-manager` and
`sudo python3 topology.py` as subprocesses) but in plain `subprocess`
so the benchmark stays a pure CLI tool with no Qt dependency.

Lifecycle
---------
1. Prompt for sudo password once (or accept via env var / passwordless).
2. `sudo mn -c` to clear any leftover OVS state.
3. Start controller #1 (e.g. active_inference_dynamic.py) — no sudo.
4. Start `sudo python3 topology.py --spec <spec>` — password to stdin.
5. Wait for state.json + FlowRPCServer to come up.
6. Run all scenarios × N trials for controller #1.
7. Kill controller #1, start controller #2 (reactive_dynamic.py).
8. Wait for switches to reconnect (state.json shows switches again).
9. Run all scenarios × N trials for controller #2.
10. Stop everything cleanly (controller SIGTERM, mininet `exit` over stdin,
    `sudo mn -c` final cleanup).
11. Generate comparison plots + summary.

All child-process stdout/stderr is streamed to the console with
`[ryu]` / `[mn]` / `[cleanup]` prefixes so the user can see what's
happening (matching app.py's Logs Console behaviour, just to stdout
instead of a Qt dock).

Ctrl-C at any point triggers a clean shutdown.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from benchmark.recorder import TrialRecorder, TrialSamples, save_trial
from benchmark.rpc_client import FlowRPCClient, server_reachable
from benchmark.scenarios import SCENARIOS, Scenario, get_scenario
from benchmark.state_poller import read_state
from benchmark.runner import DEFAULT_SCENARIO_NAMES, CONTROLLERS

_log = logging.getLogger(__name__)


# Controller module filename for each name.
CONTROLLER_MODULES = {
    "active_inference": "active_inference_dynamic.py",
    "reactive": "reactive_dynamic.py",
}

# Default topology spec — relative to project dir.
DEFAULT_SPEC = "topology_spec.json"

# How long to wait (seconds) for various transitions.
WAIT_INFRASTRUCTURE_TIMEOUT = 90.0       # state.json + RPC after start
WAIT_SWITCH_RECONNECT_TIMEOUT = 60.0    # switches to re-appear after controller switch
WAIT_CONTROLLER_STOP = 5.0              # SIGTERM grace before SIGKILL
WAIT_MININET_STOP = 8.0                 # `exit` over stdin before terminate
WAIT_MN_CLEANUP = 15.0                  # sudo mn -c timeout


# ── Process wrapper ────────────────────────────────────────────────────────


@dataclass
class ChildProcess:
    """A tracked child process with stdout/stderr streaming."""

    name: str                       # "ryu" / "mn" / "cleanup"
    proc: subprocess.Popen
    reader_thread: Optional[threading.Thread] = None
    stop_requested: bool = False

    def stop(self, timeout: float = 5.0) -> int:
        """Send SIGTERM, then SIGKILL after `timeout` seconds."""
        if self.proc.poll() is not None:
            return self.proc.returncode
        self.stop_requested = True
        try:
            self.proc.terminate()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
            except OSError:
                pass
            return self.proc.wait(timeout=2.0)

    def send_stdin(self, line: str) -> None:
        """Send a line to the process's stdin (used for `exit` to Mininet CLI)."""
        if self.proc.poll() is not None or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write((line + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass


def _stream_reader(child: ChildProcess, prefix: str) -> None:
    """Background thread: pipe child's stdout to our stdout with a prefix."""
    if child.proc.stdout is None:
        return
    for raw in iter(child.proc.stdout.readline, b""):
        try:
            line = raw.decode(errors="replace").rstrip()
        except Exception:
            continue
        if not line:
            continue
        # Print with prefix, but don't spam — limit very chatty log levels.
        if _should_show_line(child.name, line):
            print(f"  [{prefix}] {line}", flush=True)


def _should_show_line(name: str, line: str) -> bool:
    """Filter out DEBUG lines and other noise to keep the console readable."""
    if name == "ryu":
        # Suppress Ryu's verbose debug chatter.
        if "DEBUG" in line or "hub:" in line:
            return False
    if name == "mn":
        # Suppress Mininet's connection spam.
        if "Connecting to remote controller" in line and "..." in line:
            return False
    return True


# ── AutoBenchmarkRunner ─────────────────────────────────────────────────────


class AutoBenchmarkRunner:
    """
    Single-command benchmark runner.

    Usage
    -----
        runner = AutoBenchmarkRunner(trials=3, output_dir="bench_results")
        runner.run(
            controllers=["active_inference", "reactive"],
            scenarios=["baseline", "forced_bottleneck", "load_ramp", "bursty_threshold"],
        )

    This starts Ryu + Mininet itself, runs N trials per (controller,
    scenario), switches controllers mid-run, then generates the
    comparison plots + summary.

    The sudo password is requested once via getpass() (or supplied via
    the SDN_SUDO_PASSWORD env var, or skipped if passwordless sudo is
    configured — checked automatically on first `mn -c` call).
    """

    def __init__(
        self,
        trials: int = 3,
        output_dir: str = "bench_results",
        trials_dir: Optional[str] = None,
        plots_dir: Optional[str] = None,
        spec_path: Optional[str] = None,
        project_dir: Optional[str] = None,
        python_bin: str = "python3",
        ryu_manager_bin: str = "ryu-manager",
        sudo_password: Optional[str] = None,
        state_path: Optional[str] = None,
    ) -> None:
        self.trials = max(1, int(trials))
        self.output_dir = output_dir
        self.trials_dir = trials_dir or os.path.join(output_dir, "trials")
        self.plots_dir = plots_dir or os.path.join(output_dir, "plots")
        self.project_dir = project_dir or os.getcwd()
        self.python_bin = python_bin
        self.ryu_manager_bin = ryu_manager_bin

        # Resolve spec path relative to project_dir if not absolute.
        if spec_path is None:
            spec_path = os.path.join(self.project_dir, DEFAULT_SPEC)
        elif not os.path.isabs(spec_path):
            spec_path = os.path.join(self.project_dir, spec_path)
        self.spec_path = spec_path

        # state.json path — controller writes it to cwd, so use project_dir.
        self.state_path = state_path or os.path.join(self.project_dir, "state.json")

        # Sudo password handling.
        self._sudo_password: Optional[str] = sudo_password
        self._sudo_passwordless: Optional[bool] = None  # cached check

        # Child processes currently running.
        self._controller_proc: Optional[ChildProcess] = None
        self._mininet_proc: Optional[ChildProcess] = None

        os.makedirs(self.trials_dir, exist_ok=True)
        os.makedirs(self.plots_dir, exist_ok=True)

        # Register SIGINT for clean shutdown.
        signal.signal(signal.SIGINT, self._sigint_handler)
        signal.signal(signal.SIGTERM, self._sigint_handler)

    # ── Public API ─────────────────────────────────────────────────────

    def run(
        self,
        controllers: Optional[List[str]] = None,
        scenarios: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, List[TrialSamples]]]:
        """
        Run the full benchmark in one command.

        Spawns Ryu + Mininet, runs N trials per (controller, scenario),
        switches controllers mid-run, then generates plots + summary.
        """
        controllers = controllers or list(CONTROLLERS)
        scenarios = scenarios or list(DEFAULT_SCENARIO_NAMES)
        for c in controllers:
            if c not in CONTROLLER_MODULES:
                raise ValueError(
                    f"Unknown controller {c!r}. Valid: {list(CONTROLLER_MODULES)}"
                )

        # Verify tools exist before starting.
        self._check_tools_available()

        # Get sudo password if needed.
        self._ensure_sudo_password()

        all_trials: Dict[str, Dict[str, List[TrialSamples]]] = {
            c: {} for c in controllers
        }

        try:
            # ── Phase 0: cleanup any leftover state ──────────────────────
            print()
            print("=" * 72)
            print("  Phase 0: cleanup leftover Mininet/OVS state")
            print("=" * 72)
            self._cleanup_mininet()

            # ── Phase 1: start Mininet (we'll start controllers as needed)
            print()
            print("=" * 72)
            print("  Phase 1: starting Mininet network")
            print("=" * 72)
            self._start_mininet()

            # ── Phase 2+: per-controller loop
            for ctrl_idx, controller in enumerate(controllers):
                ctrl_label = f"Phase {ctrl_idx + 2}: controller = {controller}"
                print()
                print("=" * 72)
                print(f"  {ctrl_label}")
                print("=" * 72)

                # If this is NOT the first controller, clear ALL OVS flow
                # rules and groups left by the previous controller. This is
                # critical for a fair comparison: without this, the previous
                # controller's stale flows (especially SELECT groups from
                # Active Inference's multipath) remain in the switches and
                # forward traffic WITHOUT triggering packet_in — so the new
                # controller never learns hosts and can't install its own
                # flows. Result: iperf3 traffic flows through stale rules
                # (or gets dropped), and no flow_stats events are produced.
                if ctrl_idx > 0:
                    self._clear_ovs_flows()

                module = CONTROLLER_MODULES[controller]

                # Start this controller.
                self._start_controller(module)

                # Wait for state.json + RPC to come up.
                if ctrl_idx == 0:
                    # First controller: full wait (state.json doesn't exist yet).
                    self._wait_for_infrastructure()
                else:
                    # Subsequent controller: state.json already exists, we
                    # just need to wait for the new controller to populate
                    # the topology (switches re-discovered via LLDP).
                    self._wait_for_switch_reconnect()

                # Run trials.
                all_trials[controller] = self._run_trials_for_controller(
                    controller, scenarios
                )

                # Stop this controller before starting the next one.
                self._stop_controller()

            # ── Final phase: stop everything + generate plots ────────────
            print()
            print("=" * 72)
            print("  Final phase: stopping network + generating plots")
            print("=" * 72)
            self._stop_mininet()
            self._cleanup_mininet()

        finally:
            # Always make sure children are dead.
            self._force_kill_all_children()
            # And do one final mn -c to be safe.
            try:
                self._cleanup_mininet()
            except Exception:
                pass

        # Generate plots + summary.
        self._generate_plots(all_trials)

        return all_trials

    # ── Tool availability check ────────────────────────────────────────

    def _check_tools_available(self) -> None:
        """Verify ryu-manager, python3, sudo, and the spec file all exist."""
        missing = []
        if not shutil.which(self.ryu_manager_bin):
            missing.append(self.ryu_manager_bin)
        if not shutil.which(self.python_bin):
            missing.append(self.python_bin)
        if not shutil.which("sudo"):
            missing.append("sudo")
        if not os.path.isfile(self.spec_path):
            missing.append(f"spec file: {self.spec_path}")
        if missing:
            print()
            print("  [ERROR] Required tools/files not found:")
            for m in missing:
                print(f"    - {m}")
            print()
            print("  Install Mininet + Ryu, or run with --manual to drive")
            print("  an already-running controller + network.")
            raise SystemExit(1)
        print(f"  Tools OK: {self.ryu_manager_bin}, {self.python_bin}, sudo")
        print(f"  Spec: {self.spec_path}")

    # ── Sudo password handling ─────────────────────────────────────────

    def _ensure_sudo_password(self) -> None:
        """
        Get the sudo password:
          1. If SDN_SUDO_PASSWORD env var is set, use it.
          2. If passwordless sudo is configured, skip.
          3. Otherwise, prompt the user once via getpass().
        """
        env_pw = os.environ.get("SDN_SUDO_PASSWORD")
        if env_pw is not None:
            self._sudo_password = env_pw
            print("  Sudo password: from SDN_SUDO_PASSWORD env var")
            return

        # Check passwordless sudo.
        if self._check_passwordless_sudo():
            self._sudo_passwordless = True
            self._sudo_password = ""
            print("  Sudo: passwordless configured (no password needed)")
            return

        # Prompt.
        print()
        print("  Sudo password required for Mininet (the network child")
        print("  process runs as root; the controller and benchmark itself")
        print("  do not). Leave blank if you have passwordless sudo.")
        try:
            pw = getpass.getpass("  [sudo] password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(0)
        self._sudo_password = pw

    def _check_passwordless_sudo(self) -> bool:
        """Run `sudo -n true` to detect passwordless sudo. Cached."""
        if self._sudo_passwordless is not None:
            return self._sudo_passwordless
        try:
            r = subprocess.run(
                ["sudo", "-n", "true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5.0,
            )
            self._sudo_passwordless = (r.returncode == 0)
        except (subprocess.TimeoutExpired, OSError):
            self._sudo_passwordless = False
        return self._sudo_passwordless

    def _run_sudo(self, args: List[str], timeout: float = 30.0) -> int:
        """Run a sudo command with the cached password (via stdin)."""
        cmd = ["sudo", "-S", "-p", ""] + args
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.project_dir,
            )
        except OSError as exc:
            print(f"  [cleanup] failed to spawn: {exc}")
            return -1
        # Feed password.
        try:
            proc.stdin.write((self._sudo_password + "\n").encode())
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        # Stream output.
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if line:
                print(f"  [cleanup] {line}", flush=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
        return proc.returncode

    # ── Mininet lifecycle ──────────────────────────────────────────────

    def _cleanup_mininet(self) -> None:
        """Run `sudo mn -c` to clear any leftover OVS state."""
        print("  Running `sudo mn -c`...")
        self._run_sudo(["mn", "-c"], timeout=WAIT_MN_CLEANUP)

    def _start_mininet(self) -> None:
        """Start `sudo python3 topology.py --spec <spec>` as a subprocess."""
        if self._mininet_proc is not None:
            return
        cmd = [
            "sudo", "-S", "-p", "",
            self.python_bin, "topology.py", "--spec", self.spec_path,
        ]
        print(f"  Starting: {' '.join(cmd[:4])} ... {self.python_bin} topology.py --spec {os.path.basename(self.spec_path)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.project_dir,
                # Pre-feed sudo password — we'll write it after start.
                bufsize=-1,
                universal_newlines=False,
            )
        except OSError as exc:
            print(f"  [ERROR] Failed to start Mininet: {exc}")
            raise SystemExit(1)

        child = ChildProcess(name="mn", proc=proc)
        reader = threading.Thread(
            target=_stream_reader, args=(child, "mn"),
            name="mn-reader", daemon=True,
        )
        child.reader_thread = reader
        reader.start()

        # Feed sudo password.
        try:
            proc.stdin.write((self._sudo_password + "\n").encode())
            proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass

        self._mininet_proc = child

        # Wait a moment for the process to be alive (i.e. sudo auth succeeded).
        time.sleep(2.0)
        if proc.poll() is not None:
            print(f"  [ERROR] Mininet exited immediately (code {proc.returncode})")
            print("  Most likely: sudo password wrong, or another mininet is running.")
            print("  Run `sudo mn -c` manually and retry.")
            raise SystemExit(1)
        print("  Mininet started (sudo auth OK, network booting...)")

    def _stop_mininet(self) -> None:
        """Stop Mininet: send `exit` to its CLI over stdin, then terminate."""
        if self._mininet_proc is None:
            return
        child = self._mininet_proc
        print("  Stopping Mininet (sending 'exit' to CLI)...")
        child.send_stdin("exit")
        try:
            child.proc.wait(timeout=WAIT_MININET_STOP)
        except subprocess.TimeoutExpired:
            print(f"  Mininet didn't exit in {WAIT_MININET_STOP}s — terminating")
            child.stop(timeout=3.0)
        self._mininet_proc = None
        print("  Mininet stopped")

    # ── Controller lifecycle ───────────────────────────────────────────

    def _clear_ovs_flows(self) -> None:
        """
        Clear ALL OVS flow rules and groups from every switch in the topology.

        Called between controller switches to ensure the second controller
        starts from the same clean slate as the first. Without this, the
        first controller's stale flows (especially Active Inference's
        SELECT groups) remain in the switches and:
          1. Forward traffic without triggering packet_in, so the new
             controller never learns hosts.
          2. Reference group IDs that the new controller doesn't know about.
          3. Cause iperf3 traffic to either flow through stale rules (no
             measurement of the new controller's actual behaviour) or get
             dropped (no throughput samples at all).

        Uses `sudo ovs-ofctl -O OpenFlow13 del-flows` and `del-groups` for
        each switch. Switch names are read from the topology spec.
        """
        import json as _json
        # Read switch names from the topology spec.
        try:
            with open(self.spec_path) as fh:
                spec = _json.load(fh)
        except (OSError, _json.JSONDecodeError) as exc:
            print(f"  [WARN] Could not read topology spec for OVS cleanup: {exc}")
            return
        switches = [s["id"] for s in spec.get("switches", [])]
        if not switches:
            print("  [WARN] No switches found in topology spec for OVS cleanup")
            return

        print(f"  Clearing OVS flow rules + groups on {len(switches)} switches...")
        for sw in switches:
            # del-flows with no match deletes ALL flows on the switch.
            # del-groups with no group_id deletes ALL groups.
            self._run_sudo(
                ["ovs-ofctl", "-O", "OpenFlow13", "del-flows", sw],
                timeout=5.0,
            )
            self._run_sudo(
                ["ovs-ofctl", "-O", "OpenFlow13", "del-groups", sw],
                timeout=5.0,
            )
        print("  OVS flow tables cleared.")

    def _start_controller(self, module: str) -> None:
        """Start `ryu-manager --observe-links <module>` as a subprocess."""
        if self._controller_proc is not None:
            return
        env = os.environ.copy()
        env["SDN_TOPOLOGY_SPEC"] = self.spec_path
        # Set the trial metadata file path — the controller reads this
        # file on every event-log call to know which trial is currently
        # running (since the controller is spawned once, but multiple
        # trials run against it). The benchmark runner writes to this
        # file before each trial.
        env["SDN_TRIAL_META_PATH"] = os.path.join(
            self.output_dir, "results", "_trial_metadata.json"
        )
        # Set the approach name as an env-var fallback (used if the
        # metadata file doesn't exist yet — e.g. before the first trial).
        approach = "active_inference" if "active_inference" in module else "reactive"
        env["SDN_APPROACH"] = approach
        cmd = [self.ryu_manager_bin, "--observe-links", module]
        print(f"  Starting: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.project_dir,
                env=env,
                bufsize=-1,
                universal_newlines=False,
            )
        except OSError as exc:
            print(f"  [ERROR] Failed to start controller: {exc}")
            raise SystemExit(1)

        child = ChildProcess(name="ryu", proc=proc)
        reader = threading.Thread(
            target=_stream_reader, args=(child, "ryu"),
            name="ryu-reader", daemon=True,
        )
        child.reader_thread = reader
        reader.start()

        self._controller_proc = child
        print(f"  Controller started ({module})")

    def _stop_controller(self) -> None:
        """Stop the current controller (SIGTERM, then SIGKILL)."""
        if self._controller_proc is None:
            return
        child = self._controller_proc
        print("  Stopping controller (SIGTERM)...")
        child.stop(timeout=WAIT_CONTROLLER_STOP)
        self._controller_proc = None
        print("  Controller stopped")

    # ── Wait helpers ──────────────────────────────────────────────────

    def _wait_for_infrastructure(self, timeout: float = WAIT_INFRASTRUCTURE_TIMEOUT) -> None:
        """Wait for state.json to exist AND FlowRPCServer to be reachable."""
        print(f"  Waiting for state.json + FlowRPCServer (up to {timeout:.0f}s)...",
              end="", flush=True)
        deadline = time.time() + timeout
        state_ok = False
        rpc_ok = False
        last_dot = time.time()
        while time.time() < deadline:
            if not state_ok:
                st = read_state(self.state_path)
                if st is not None and st.get("nodes"):
                    state_ok = True
                    print(" state.json✓", end="", flush=True)
            if not rpc_ok and server_reachable():
                rpc_ok = True
                print(" rpc✓", end="", flush=True)
            if state_ok and rpc_ok:
                print()
                return
            if time.time() - last_dot > 5:
                print(".", end="", flush=True)
                last_dot = time.time()
            time.sleep(0.5)
        print()
        raise RuntimeError(
            f"Timed out after {timeout}s (state_ok={state_ok}, rpc_ok={rpc_ok})"
        )

    def _wait_for_switch_reconnect(
        self, timeout: float = WAIT_SWITCH_RECONNECT_TIMEOUT
    ) -> None:
        """
        After a controller switch, wait for the new controller to re-discover
        all the switches via LLDP. We watch state.json's `nodes` list — when
        the switch count stops growing for 3 consecutive polls, we're done.

        3 polls (6s at POLL_INTERVAL=2s) gives the controller enough time
        to not only discover switches but also start processing LLDP and
        be ready for host learning when the first trial starts.

        Also re-confirms the RPC server is still up (Mininet never stopped).
        """
        print(f"  Waiting for switch re-discovery (up to {timeout:.0f}s)...",
              end="", flush=True)
        deadline = time.time() + timeout
        last_count = -1
        stable_for = 0
        last_dot = time.time()
        while time.time() < deadline:
            st = read_state(self.state_path)
            if st is not None:
                switches = [n for n in st.get("nodes", [])
                            if n.get("type") == "switch"]
                count = len(switches)
                if count > 0:
                    if count == last_count:
                        stable_for += 1
                        if stable_for >= 3:
                            print(f" {count} switches✓")
                            # Also confirm RPC is still up.
                            if server_reachable():
                                print("  rpc✓")
                                return
                            else:
                                print("  [WARN] RPC server not reachable!")
                                return
                    else:
                        stable_for = 0
                        last_count = count
            if time.time() - last_dot > 5:
                print(".", end="", flush=True)
                last_dot = time.time()
            time.sleep(2.0)  # match POLL_INTERVAL
        print()
        raise RuntimeError(
            f"Switches didn't re-discover within {timeout}s"
        )

    # ── Trial execution ────────────────────────────────────────────────

    def _run_trials_for_controller(
        self, controller: str, scenario_names: List[str]
    ) -> Dict[str, List[TrialSamples]]:
        """Run N trials per scenario for the given controller.

        Saves each trial's raw samples to:
            results/<scenario>/<controller>/raw/trialNN.json
        and the structured reroute event log to:
            results/<scenario>/<controller>/raw/trialNN_events.jsonl
        """
        result: Dict[str, List[TrialSamples]] = {}
        rpc = FlowRPCClient()
        try:
            rpc.connect()
        except Exception as exc:
            print(f"  [ERROR] Could not connect to FlowRPCServer: {exc}")
            return result

        try:
            for scn_name in scenario_names:
                scenario = get_scenario(scn_name)
                if scenario is None:
                    print(f"  [WARN] Unknown scenario {scn_name!r}, skipping")
                    continue
                print()
                print(f"  Scenario: {scn_name} "
                      f"({len(scenario.steps)} flows, "
                      f"~{scenario.monitor_duration:.0f}s/trial)")
                # Per-scenario, per-controller raw output dir.
                trial_dir = os.path.join(
                    self.output_dir, "results", scn_name, controller, "raw"
                )
                os.makedirs(trial_dir, exist_ok=True)
                trial_list: List[TrialSamples] = []
                for trial_idx in range(self.trials):
                    print(f"    trial {trial_idx + 1}/{self.trials}... ",
                          end="", flush=True)
                    # Per-trial event log path. The controller (running as
                    # a subprocess inherited from auto_runner) reads
                    # SDN_EVENT_LOG_PATH from the env to know where to write.
                    event_log_path = os.path.join(
                        trial_dir, f"trial{trial_idx:02d}_events.jsonl"
                    )
                    try:
                        rec = TrialRecorder(
                            controller=controller,
                            scenario=scenario,
                            trial=trial_idx,
                            rpc_client=rpc,
                            state_path=self.state_path,
                            event_log_path=event_log_path,
                        )
                        samples = rec.run()
                        save_trial(samples, trial_dir)
                        trial_list.append(samples)
                        # Count reroute events by type for quick visibility.
                        n_succ = sum(
                            1 for e in samples.reroute_events
                            if e.success and e.trigger_reason in
                            ("congestion_threshold_crossed", "efe_switch")
                        )
                        n_fail = sum(
                            1 for e in samples.reroute_events
                            if not e.success
                        )
                        # Count benchmark flow start failures (exclude warmup).
                        n_flow_fail = sum(
                            1 for r in samples.flow_start_results
                            if not r.get("ok") and not r.get("warmup")
                        )
                        n_warmup_fail = sum(
                            1 for r in samples.flow_start_results
                            if not r.get("ok") and r.get("warmup")
                        )
                        print(
                            f"done "
                            f"(links={len(samples.link_samples)}, "
                            f"flows={len(samples.flow_samples)}, "
                            f"throughput={len(samples.throughput_samples)}, "
                            f"reroutes={len(samples.reroute_events)} "
                            f"[succ={n_succ}, fail={n_fail}], "
                            f"flow_starts: ok={len(samples.flow_ids)}, "
                            f"fail={n_flow_fail}, warmup_fail={n_warmup_fail})"
                        )
                        # If throughput is zero, print diagnostic info so
                        # the user can see what iperf3 actually output
                        # (or whether it ran at all).
                        if len(samples.throughput_samples) == 0:
                            print(f"    [WARN] Zero throughput samples!")
                            print(f"    Diagnostic: {samples.notes}")
                            # Show flow_start_results so the user sees the
                            # exact error from start_flow (e.g. "flow id
                            # 'base' already exists" — the original bug).
                            if samples.flow_start_results:
                                print(f"    flow_start_results:")
                                for r in samples.flow_start_results:
                                    tag = "WARMUP" if r.get("warmup") else "BENCH"
                                    status = "ok" if r.get("ok") else f"FAIL: {r.get('error')}"
                                    print(f"      [{tag}] {r.get('flow_id')}: {status}")
                            print(f"    Possible causes:")
                            print(f"      - Stale flow IDs in FlowController (fixed — should not happen)")
                            print(f"      - iperf3 failed to connect (flows not installed)")
                            print(f"      - Controller didn't learn hosts (check warmup)")
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"FAILED ({exc})")
                        _log.exception("trial failed")
                result[scn_name] = trial_list
        finally:
            rpc.close()
        return result

    # ── Plot generation + sanity checks ─────────────────────────────────

    def _generate_plots(
        self, all_trials: Dict[str, Dict[str, List[TrialSamples]]]
    ) -> None:
        """Run sanity checks, then call the plotter to generate comparison
        plots + summary.

        Sanity check failures (errors) abort plot generation — the user
        must fix the underlying measurement problem before the plots
        can be trusted. Warnings are logged but plotting continues.
        """
        from benchmark.sanity_check import check_trial_consistency, print_issues
        from benchmark.plotter import generate_comparison_plots

        print()
        print("  Running sanity checks...")
        issues = check_trial_consistency(all_trials)
        print_issues(issues)

        errors = [i for i in issues if i.level == "error"]
        if errors:
            print()
            print(f"  [ABORT] {len(errors)} sanity-check error(s) — "
                  f"plot generation skipped.")
            print("  Fix the measurement problem above before trusting any plot.")
            return

        print()
        print("  Generating comparison plots + summary...")
        files = generate_comparison_plots(all_trials, self.plots_dir)
        for name, path in files.items():
            print(f"    {name}: {path}")
        print()
        print(f"  Done. Open {self.plots_dir}/summary.md for the tradeoff analysis.")

    # ── Cleanup ────────────────────────────────────────────────────────

    def _force_kill_all_children(self) -> None:
        """Force-kill any still-running child processes (cleanup safety net)."""
        if self._controller_proc is not None:
            try:
                self._controller_proc.stop(timeout=2.0)
            except Exception:
                pass
            self._controller_proc = None
        if self._mininet_proc is not None:
            try:
                self._mininet_proc.stop(timeout=2.0)
            except Exception:
                pass
            self._mininet_proc = None

    def _sigint_handler(self, signum, frame) -> None:
        """Ctrl-C handler: clean shutdown."""
        print()
        print("  [Interrupted] Cleaning up child processes...")
        self._force_kill_all_children()
        try:
            self._cleanup_mininet()
        except Exception:
            pass
        print("  Cleanup done.")
        sys.exit(130)
