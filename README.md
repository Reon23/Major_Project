# SDN Active-Inference Demo

An SDN network (Mininet + Ryu) whose controller uses active inference
(expected free energy minimisation) to route flows, with a live PyQt6
visualizer.

## Quick start — one app

```
nix develop      # enters the shell with mininet/ryu/PyQt6/etc. on PATH
python3 app.py
```

This single process:

- lets you design/edit the network topology (hosts, switches, links,
  bandwidth, delay) in the **Topology Editor** panel and hit **Apply**;
- starts and supervises the Ryu controller and the Mininet network as
  child processes from the **Control Panel** (status indicators, live
  logs in the **Logs Console** panel);
- lets you generate and monitor traffic between any two hosts from the
  **Traffic Generator** panel — pick a source/destination, protocol
  (UDP/TCP), target bandwidth, and duration, then **Start Flow**. Multiple
  flows between different (or the same) host pairs can run concurrently,
  each with its own live throughput readout and its own **Stop** button
  (or **Stop All Flows**);
- shows the same live topology/traffic view as the old standalone
  visualizer.

Mininet needs root. The app itself runs as your normal user; only the
Mininet child process is escalated via `sudo`. Enter your sudo password
once in the Control Panel's password field before starting the network
(leave it blank if this machine has a passwordless-sudo rule for this
script). Closing the app — or **Stop Everything** — always cleans up
Mininet/OVS state (the `sudo mn -c` equivalent), so you never need to run
that by hand before the next launch.

### Typical flow

1. `python3 app.py`
2. (Optional) Open the **Topology Editor**, change hosts/switches/links,
   click **Apply** — this validates the graph and saves it to
   `topology_spec.json`, the single source of truth both the Mininet
   launcher and the controller read (including real link bandwidth, so
   utilisation is computed against your configured capacity, not a
   hardcoded default).
3. In the **Control Panel**, enter your sudo password and click
   **Start Everything** (or start the controller and network separately).
4. Watch the live topology/traffic view. Use the **Traffic Generator**
   panel to send iperf3 traffic between any two hosts — set protocol,
   bandwidth, and duration, click **Start Flow**; start as many
   concurrent flows between different host pairs as you like, and stop
   any one (or all of them) at any time. A terminal Mininet CLI (fallback
   path below) still works too if you want `pingall`.
5. Change the topology and click **Apply** again at any time — this tears
   down and relaunches only the Mininet child; the controller keeps
   running and re-discovers the new graph via LLDP. Any traffic flows
   still running at that point are torn down along with the network.
6. Close the window (or **Stop Everything**) when done.

## Fallback — three manual terminals

Each piece still runs standalone exactly as before, which is useful for
debugging one layer in isolation.

```
# Terminal 1 — controller (topology-independent; discovers switches/links via LLDP)
ryu-manager --observe-links active_inference_dynamic.py

# Terminal 2 — network (needs root; uses the built-in default topology
# unless you pass --spec)
sudo mn -c
sudo python3 topology.py
# or: sudo python3 topology.py --spec topology_spec.json

# Terminal 3 — visualizer (polls state.json once a second)
python3 dynamic_visualizer.py
# or: python3 dynamic_visualizer.py --state /path/to/state.json
```

Inside the Mininet CLI (Terminal 2): `pingall`, `h1 ping h2`,
`iperf h1 h2`, etc.

## Topology spec format

Both `topology.py` and `active_inference_dynamic.py` (via the
`SDN_TOPOLOGY_SPEC` environment variable, defaulting to
`topology_spec.json` in the working directory) read the same JSON file —
see `sdn/topology_spec.py` for the schema, validation rules, and the
built-in default (today's 6-host/4-switch topology):

```json
{
  "controller": { "ip": "127.0.0.1", "port": 6633 },
  "switches": [{ "id": "s1" }, { "id": "s2" }],
  "hosts": [{ "id": "h1", "ip": "10.0.0.1/24" }],
  "links": [
    { "src": "h1", "dst": "s1", "bw": 50, "delay": "1ms" },
    { "src": "s1", "dst": "s2", "bw": 10, "delay": "10ms", "max_queue_size": 50 }
  ]
}
```

Constraints (validated by `sdn/topology_spec.validate_spec`, and enforced
live by the Topology Editor):

- switch ids must look like `s1`, `s2`, ... (Mininet derives the OpenFlow
  dpid from the trailing number; the rest of the codebase assumes
  `dpid == N`); host ids must look like `h1`, `h2`, ...
- host IPs must be inside `10.0.0.0/24` (multi-subnet addressing isn't
  supported yet — see "Known limitations")
- no duplicate ids, no dangling links, no host-to-host links, every host
  must connect to at least one switch
- a switch graph that isn't fully connected is a warning, not an error

## Architecture

- **`app.py`** — the orchestrator entry point. Extends
  `dynamic_visualizer.MainWindow` with a Control Panel dock, a Topology
  Editor dock, a Traffic Generator dock, and a Logs Console dock.
- **`process_manager.py`** — `QProcess`-based lifecycle management for the
  `ryu-manager` and Mininet child processes (start/stop/restart, log
  streaming, sudo escalation for Mininet only, `mn -c` cleanup on
  stop/apply/shutdown).
- **`topology_editor.py`** — `TopologyEditorModel` (plain-Python graph
  model, no Qt — testable standalone) plus the `QGraphicsScene`-based
  visual editor and dock widget.
- **`control_panel.py`** — the Control Panel and Logs Console dock
  widgets.
- **`traffic_manager.py`** / **`traffic_panel.py`** — the Traffic
  Generator dock (`TrafficPanel`) and its `QTcpSocket` client
  (`TrafficManager`), which talks to a small JSON-line RPC server embedded
  in the Mininet child process (`topology.py`'s `FlowRPCServer` — see
  `sdn/traffic_protocol.py` for the wire format). This exists because host
  network namespaces (and the ability to run `iperf3` inside them) only
  exist inside the Mininet process, so driving traffic generation from
  the GUI needs a structured, concurrency-safe channel into that process
  rather than scraping the Mininet CLI's text output.
- **`sdn/topology_spec.py`** — the shared topology spec schema, validator,
  and default, imported by both `topology.py` and
  `active_inference_dynamic.py`.
- **`topology.py`** — now a generic, spec-driven Mininet topology builder
  (`SpecTopo`) instead of a hardcoded class, plus the embedded
  `FlowRPCServer`/`FlowController` that runs/stops/reports on iperf3
  flows between hosts on request; `--spec <path>` or the built-in default.
- **`active_inference_dynamic.py`** / **`sdn/`** / **`ai/`** — the Ryu
  controller and its active-inference routing logic. Unchanged in
  behaviour except:
  - it now seeds real link `capacity_mbps` from the topology spec instead
    of always assuming `DEFAULT_LINK_BW_MBPS` (10 Mbps) for every
    switch-switch link, and re-reads the spec file each monitor tick so
    an "Apply topology" from the GUI is picked up live, without
    restarting the controller;
  - the blockchain-layer `CONTROLLER_DOMAINS` split (which controller
    "identity" owns which switches) is now computed dynamically from
    whatever dpids are actually discovered, instead of a hardcoded
    `{1: {1,2}, 2: {3,4}}` map that only matched exactly 4 switches.
- **`dynamic_visualizer.py`** — unchanged; the topology/traffic canvas and
  side panel `app.py` builds on top of.

## Known limitations / out of scope (see task doc §7)

- The `blockchain/` package (model-trading + audit ledger) doesn't exist
  in this repo; that layer is disabled by a caught `ImportError` and the
  ledger panel stays empty. Not addressed here.
- Host IPs outside `10.0.0.0/24` aren't supported — flagged as a possible
  follow-up, not implemented.
- No in-app Mininet CLI (`pingall`/`iperf` from inside the GUI) yet beyond
  the Traffic Generator's flow-based `iperf3` — use a terminal against the
  running Mininet process, or the fallback three-terminal workflow, for
  `pingall` or other ad-hoc CLI commands.
- Linux/Nix only, same as before — Mininet requires Linux.
