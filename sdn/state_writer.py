"""
sdn/state_writer.py — Atomic state.json export for the PyQt visualizer.

Schema
------
{
  "timestamp": "...",
  "nodes": [
    {"id": "s1", "type": "switch"},
    {"id": "h_10.0.0.1", "type": "host", "ip": "10.0.0.1"}
  ],
  "links": [
    {"src": "s1", "dst": "s2", "src_port": 2, "dst_port": 1,
     "rate_mbps": 4.52, "util": 0.452, "capacity_mbps": 10,
     "loss_fraction": 0.012},

    # Host-switch edges (no util data — drawn as plain topology edges):
    {"src": "h_10.0.0.1", "dst": "s1", "host_link": true,
     "rate_mbps": 0.0, "util": 0.0}
  ],
  "flows": [
    {"src_ip": "...", "dst_ip": "...", "path": ["s1","s3","s4"],
     "G": 0.42, "rerouted": true}
  ],
  "event": "...",

  # Optional — only present when the blockchain audit ledger is enabled.
  # Existing readers that ignore unknown keys continue to work unchanged.
  "ledger": {
    "chain_length": 42,
    "latest_block_hash": "ab12…",
    "latest_block_index": 41,
    "last_transactions": [ {index, tx_type, timestamp, payload, hash}, ... ]
  }
}
"""

import datetime
import json
import logging
import os

from ai.policy import compute_efe_for_path

# Allow constants.py to omit STATE_JSON_PATH without import error
try:
    from sdn.constants import STATE_JSON_PATH
except ImportError:
    STATE_JSON_PATH = "state.json"

_log = logging.getLogger(__name__)


def write_state(
    topology_manager,
    host_manager,
    flows: dict,  # (src_ip, dst_ip) -> flow_info dict
    event: str = "",
    path: str = STATE_JSON_PATH,
    ledger=None,
) -> None:
    """
    Atomically write current network state to `path`.
    Uses temp-file + os.replace so the visualiser never reads a partial file.

    Parameters
    ----------
    ledger : optional
        If provided (a `blockchain.ledger.Ledger` instance), a compact
        summary is added under the top-level "ledger" key. Existing
        readers that ignore unknown keys keep working unchanged.
    """
    # ── Nodes ─────────────────────────────────────────────────────────────────
    nodes = []
    for dpid in topology_manager.get_all_switch_dpids():
        nodes.append({"id": f"s{dpid}", "type": "switch"})
    nodes.extend(host_manager.all_hosts_for_state())

    # ── Links ─────────────────────────────────────────────────────────────────
    # Switch-to-switch links with full utilisation data (unchanged)
    links = topology_manager.get_all_links_for_state()

    # Host-to-switch links: derived from host_manager location table.
    # These carry no utilisation data — the visualizer draws them as plain
    # grey topology edges so the full graph is visible.
    host_locations = host_manager.get_all_locations()  # ip -> (dpid, port)
    for ip, (dpid, _port) in host_locations.items():
        links.append(
            {
                "src": f"h_{ip}",
                "dst": f"s{dpid}",
                "host_link": True,  # flag so the visualizer skips util coloring
                "rate_mbps": 0.0,
                "util": 0.0,
            }
        )

    # ── Flows ─────────────────────────────────────────────────────────────────
    flows_out = []
    for (src_ip, dst_ip), flow_info in flows.items():
        path_dpids = flow_info.get("path", [])
        path_labels = [f"s{d}" for d in path_dpids]
        active_idx = flow_info.get("path_idx", 0)
        beliefs = flow_info.get("beliefs", {})
        load_est = flow_info.get("load_estimate", 0.0)

        G = (
            compute_efe_for_path(
                path_idx=active_idx,
                active_idx=active_idx,
                beliefs=beliefs,
                load_delta=load_est,
            )
            if beliefs
            else 0.0
        )

        flows_out.append(
            {
                "src_ip": src_ip,
                "dst_ip": dst_ip,
                "path": path_labels,
                "G": round(G, 5),
                "rerouted": "REROUTED" in event and src_ip in event,
            }
        )

    state = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "nodes": nodes,
        "links": links,
        "flows": flows_out,
        "event": event,
    }

    # ── Optional ledger summary (additive — unknown-key-tolerant readers
    #    continue to work unchanged). ────────────────────────────────────────
    if ledger is not None:
        try:
            state["ledger"] = ledger.summary_for_state(last_n=10)
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning("state_writer: ledger summary failed: %s", exc)

    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        _log.warning("Could not write %s: %s", path, exc)
