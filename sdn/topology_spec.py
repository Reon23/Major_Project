"""
sdn/topology_spec.py — Shared topology specification: schema, validation,
load/save, and the built-in default spec (today's 6-host/4-switch topology).

This module is imported by BOTH:
  - topology.py            (the Mininet launcher — builds a Topo from a spec)
  - active_inference_dynamic.py (the Ryu controller — reads link "bw" so
                             utilisation is computed against the real
                             configured capacity instead of a hardcoded
                             fallback; see sdn/constants.DEFAULT_LINK_BW_MBPS)

Keeping this in one place means the topology-launcher and the controller can
never silently disagree about what a switch/host/link is named or how much
bandwidth a link has.

Spec JSON schema
-----------------
{
  "controller": {"ip": "127.0.0.1", "port": 6633},
  "switches": [{"id": "s1"}, {"id": "s2"}, ...],
  "hosts": [{"id": "h1", "ip": "10.0.0.1/24"}, ...],
  "links": [
    {"src": "h1", "dst": "s1", "bw": 50, "delay": "1ms"},
    {"src": "s1", "dst": "s2", "bw": 10, "delay": "10ms", "max_queue_size": 50}
  ]
}

Switch ids MUST be of the form "sN" (N a positive int) — Mininet assigns the
OpenFlow datapath id from that trailing number by default
(mininet.node.Switch.defaultDpid), and the rest of this codebase (state.json
node ids, CONTROLLER_DOMAINS, etc.) assumes dpid == N. Host ids must be
"hN". This is a documented constraint of the existing system, not something
introduced here.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from typing import Optional

SWITCH_ID_RE = re.compile(r"^s(\d+)$")
HOST_ID_RE = re.compile(r"^h(\d+)$")

# Default location both the launcher and the controller look for the spec,
# unless overridden (CLI --spec for the launcher, SDN_TOPOLOGY_SPEC env var
# for the controller — see active_inference_dynamic.py).
DEFAULT_SPEC_PATH = "topology_spec.json"

# Must match utils.ip_utils.MININET_SUBNET / sdn.constants.MININET_SUBNET.
# Multi-subnet host addressing is out of scope (see task doc §7); this is
# the one place the editor's IP field constraint should be derived from.
DEFAULT_SUBNET_PREFIX = "10.0.0."
DEFAULT_SUBNET_CIDR = "10.0.0.0/24"


class TopologySpecError(ValueError):
    """Raised when a topology spec fails validation."""


def default_spec() -> dict:
    """
    The built-in default spec: reproduces today's 6-host/4-switch topology
    from topology.py's hardcoded DynamicTopo, as plain data.
    """
    return {
        "controller": {"ip": "127.0.0.1", "port": 6633},
        "switches": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}, {"id": "s4"}],
        "hosts": [
            {"id": "h1", "ip": "10.0.0.1/24"},
            {"id": "h2", "ip": "10.0.0.2/24"},
            {"id": "h3", "ip": "10.0.0.3/24"},
            {"id": "h4", "ip": "10.0.0.4/24"},
            {"id": "h5", "ip": "10.0.0.5/24"},
            {"id": "h6", "ip": "10.0.0.6/24"},
        ],
        "links": [
            {"src": "h1", "dst": "s1", "bw": 50, "delay": "1ms"},
            {"src": "h2", "dst": "s4", "bw": 50, "delay": "1ms"},
            {"src": "h3", "dst": "s2", "bw": 50, "delay": "1ms"},
            {"src": "h4", "dst": "s2", "bw": 50, "delay": "1ms"},
            {"src": "h5", "dst": "s3", "bw": 50, "delay": "1ms"},
            {"src": "h6", "dst": "s3", "bw": 50, "delay": "1ms"},
            {"src": "s1", "dst": "s2", "bw": 10, "delay": "10ms", "max_queue_size": 50},
            {"src": "s1", "dst": "s3", "bw": 10, "delay": "10ms", "max_queue_size": 50},
            {"src": "s2", "dst": "s4", "bw": 10, "delay": "10ms", "max_queue_size": 50},
            {"src": "s3", "dst": "s4", "bw": 10, "delay": "10ms", "max_queue_size": 50},
        ],
    }


def validate_spec(spec: dict) -> list:
    """
    Validate a topology spec. Returns a list of warning strings (non-fatal,
    e.g. "graph not fully connected"). Raises TopologySpecError on anything
    fatal (duplicate ids, dangling links, host with no switch link, bad IP).
    """
    if not isinstance(spec, dict):
        raise TopologySpecError("spec must be a JSON object")

    switches = spec.get("switches", [])
    hosts = spec.get("hosts", [])
    links = spec.get("links", [])

    if not isinstance(switches, list) or not isinstance(hosts, list) or not isinstance(
        links, list
    ):
        raise TopologySpecError("'switches', 'hosts', and 'links' must be arrays")

    if not switches:
        raise TopologySpecError("spec must define at least one switch")

    switch_ids = [s.get("id") for s in switches]
    host_ids = [h.get("id") for h in hosts]

    # ── Duplicate / malformed ids ───────────────────────────────────────────
    seen = set()
    for sid in switch_ids:
        if not sid or not SWITCH_ID_RE.match(sid):
            raise TopologySpecError(
                f"invalid switch id {sid!r} — switch ids must look like 's1', 's2', ..."
            )
        if sid in seen:
            raise TopologySpecError(f"duplicate node id: {sid!r}")
        seen.add(sid)
    for hid in host_ids:
        if not hid or not HOST_ID_RE.match(hid):
            raise TopologySpecError(
                f"invalid host id {hid!r} — host ids must look like 'h1', 'h2', ..."
            )
        if hid in seen:
            raise TopologySpecError(f"duplicate node id: {hid!r}")
        seen.add(hid)

    all_ids = set(switch_ids) | set(host_ids)

    # ── Host IPs constrained to the configured Mininet subnet ──────────────
    for h in hosts:
        ip_cidr = h.get("ip", "")
        try:
            iface = ipaddress.ip_interface(ip_cidr)
        except ValueError as exc:
            raise TopologySpecError(
                f"host {h.get('id')!r} has an invalid ip {ip_cidr!r}: {exc}"
            )
        if not str(iface.ip).startswith(DEFAULT_SUBNET_PREFIX):
            raise TopologySpecError(
                f"host {h.get('id')!r} ip {ip_cidr!r} is outside the supported "
                f"subnet {DEFAULT_SUBNET_CIDR} (multi-subnet addressing is not "
                f"supported yet)"
            )

    dup_ip = {}
    for h in hosts:
        ip_only = h.get("ip", "").split("/")[0]
        dup_ip.setdefault(ip_only, []).append(h.get("id"))
    for ip_only, owners in dup_ip.items():
        if len(owners) > 1:
            raise TopologySpecError(f"duplicate host ip {ip_only!r} used by {owners}")

    # ── Links: no dangling endpoints, no duplicate links, no self-links ────
    link_pairs = set()
    host_link_count = {hid: 0 for hid in host_ids}
    switch_switch_edges = []
    for link in links:
        src, dst = link.get("src"), link.get("dst")
        if src not in all_ids or dst not in all_ids:
            raise TopologySpecError(
                f"link {src!r}<->{dst!r} references an id not present in "
                f"switches/hosts"
            )
        if src == dst:
            raise TopologySpecError(f"link cannot connect {src!r} to itself")
        key = frozenset((src, dst))
        if key in link_pairs:
            raise TopologySpecError(f"duplicate link between {src!r} and {dst!r}")
        link_pairs.add(key)

        bw = link.get("bw")
        if bw is None or not isinstance(bw, (int, float)) or bw <= 0:
            raise TopologySpecError(
                f"link {src!r}<->{dst!r} needs a positive numeric 'bw' (Mbps)"
            )

        src_is_host = src in host_ids
        dst_is_host = dst in host_ids
        if src_is_host:
            host_link_count[src] += 1
        if dst_is_host:
            host_link_count[dst] += 1
        if not src_is_host and not dst_is_host:
            switch_switch_edges.append((src, dst))
        if src_is_host and dst_is_host:
            raise TopologySpecError(
                f"link {src!r}<->{dst!r} connects two hosts directly — hosts "
                f"must connect to a switch"
            )

    # ── Every host must connect to at least one switch ──────────────────────
    orphans = [hid for hid, n in host_link_count.items() if n == 0]
    if orphans:
        raise TopologySpecError(
            f"host(s) with no link to a switch: {orphans}"
        )

    warnings = []

    # ── Connectivity across switches is a warning, not fatal ───────────────
    if len(switch_ids) > 1:
        adj = {sid: set() for sid in switch_ids}
        for a, b in switch_switch_edges:
            adj[a].add(b)
            adj[b].add(a)
        seen_sw = set()
        stack = [switch_ids[0]]
        while stack:
            cur = stack.pop()
            if cur in seen_sw:
                continue
            seen_sw.add(cur)
            stack.extend(adj[cur] - seen_sw)
        if len(seen_sw) != len(switch_ids):
            disconnected = sorted(set(switch_ids) - seen_sw)
            warnings.append(
                f"switch graph is not fully connected — unreachable from "
                f"{switch_ids[0]}: {disconnected}"
            )

    return warnings


def dpid_of(switch_id: str) -> int:
    """'s3' -> 3. Assumes an already-validated switch id."""
    m = SWITCH_ID_RE.match(switch_id)
    if not m:
        raise TopologySpecError(f"invalid switch id: {switch_id!r}")
    return int(m.group(1))


def link_capacity_by_dpid(spec: dict) -> dict:
    """
    Return {(src_dpid, dst_dpid): bw_mbps} (both directions) for every
    switch<->switch link in the spec. Used by the controller to seed real
    link capacities instead of DEFAULT_LINK_BW_MBPS.
    """
    switch_ids = {s["id"] for s in spec.get("switches", [])}
    out = {}
    for link in spec.get("links", []):
        src, dst = link.get("src"), link.get("dst")
        if src in switch_ids and dst in switch_ids:
            bw = float(link.get("bw"))
            out[(dpid_of(src), dpid_of(dst))] = bw
            out[(dpid_of(dst), dpid_of(src))] = bw
    return out


def load_spec(path: str) -> dict:
    with open(path, "r") as fh:
        spec = json.load(fh)
    validate_spec(spec)
    return spec


def save_spec(spec: dict, path: str) -> list:
    """Validate then atomically write spec to path. Returns warnings."""
    warnings = validate_spec(spec)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(spec, fh, indent=2)
    os.replace(tmp, path)
    return warnings


def spec_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def load_spec_or_default(path: str) -> dict:
    """Best-effort load: falls back to default_spec() if path is missing
    or invalid, so both the launcher and the controller always have
    *something* usable to run standalone with."""
    try:
        if os.path.exists(path):
            return load_spec(path)
    except (OSError, json.JSONDecodeError, TopologySpecError):
        pass
    return default_spec()
