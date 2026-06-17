"""
sdn/topology_manager.py — NetworkX graph, switch/link/trunk-port management,
                           path computation.

Thread-safety: callers must hold the controller's _lock (or use the
provided lock argument) before calling mutating methods.
"""

import threading
from typing import Optional

import networkx as nx

from sdn.constants import DEFAULT_LINK_BW_MBPS, MAX_CANDIDATE_PATHS


class TopologyManager:
    """
    Maintains:
      - NetworkX graph of switches and inter-switch links
      - datapath registry  (dpid -> datapath)
      - switch-to-switch port map  _sw_port[(src, dst)] = out_port
      - trunk port set  _trunk_ports: set of (dpid, port_no)
      - per-link utilisation  _link_util[(src, dst)] = {...}
      - candidate path cache  _path_cache[(src_ip, dst_ip)] = [[dpid,...], ...]
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._graph = nx.Graph()
        self._datapaths = {}  # dpid -> datapath
        self._sw_port = {}  # (src_dpid, dst_dpid) -> out_port
        self._trunk_ports = set()  # (dpid, port_no)
        self._link_util = {}  # (src_dpid, dst_dpid) -> dict
        self._path_cache = {}  # (src_ip, dst_ip) -> [[dpid,...],...]

    # ── Switch registry ───────────────────────────────────────────────────────

    def add_switch(self, dpid: int, dp) -> None:
        with self._lock:
            self._datapaths[dpid] = dp
            if not self._graph.has_node(dpid):
                self._graph.add_node(dpid, type="switch")

    def remove_switch(self, dpid: int) -> None:
        with self._lock:
            self._datapaths.pop(dpid, None)
            if self._graph.has_node(dpid):
                self._graph.remove_node(dpid)
            # Remove trunk ports for this switch
            self._trunk_ports = {(d, p) for (d, p) in self._trunk_ports if d != dpid}
            self._path_cache.clear()

    def get_datapath(self, dpid: int):
        with self._lock:
            return self._datapaths.get(dpid)

    def all_datapaths(self) -> list:
        with self._lock:
            return list(self._datapaths.values())

    # ── Link management ───────────────────────────────────────────────────────

    def add_link(
        self, src_dpid: int, src_port: int, dst_dpid: int, dst_port: int
    ) -> None:
        with self._lock:
            self._sw_port[(src_dpid, dst_dpid)] = src_port
            self._sw_port[(dst_dpid, src_dpid)] = dst_port

            # Mark both ends as trunk ports
            self._trunk_ports.add((src_dpid, src_port))
            self._trunk_ports.add((dst_dpid, dst_port))

            if not self._graph.has_edge(src_dpid, dst_dpid):
                self._graph.add_edge(
                    src_dpid,
                    dst_dpid,
                    src_port=src_port,
                    dst_port=dst_port,
                    capacity_mbps=DEFAULT_LINK_BW_MBPS,
                    weight=1,
                )

            for key in [(src_dpid, dst_dpid), (dst_dpid, src_dpid)]:
                if key not in self._link_util:
                    self._link_util[key] = {
                        "rate_mbps": 0.0,
                        "util": 0.0,
                        "capacity_mbps": DEFAULT_LINK_BW_MBPS,
                    }

            self._path_cache.clear()

    def remove_link(
        self, src_dpid: int, src_port: int, dst_dpid: int, dst_port: int
    ) -> None:
        with self._lock:
            self._sw_port.pop((src_dpid, dst_dpid), None)
            self._sw_port.pop((dst_dpid, src_dpid), None)
            if self._graph.has_edge(src_dpid, dst_dpid):
                self._graph.remove_edge(src_dpid, dst_dpid)
            # Only remove trunk-port designation if no other link uses it
            # (safe to remove — will be re-added if link reappears)
            self._trunk_ports.discard((src_dpid, src_port))
            self._trunk_ports.discard((dst_dpid, dst_port))
            self._path_cache.clear()

    # ── Trunk port queries ────────────────────────────────────────────────────

    def is_trunk_port(self, dpid: int, port: int) -> bool:
        with self._lock:
            return (dpid, port) in self._trunk_ports

    # ── Port map queries ──────────────────────────────────────────────────────

    def get_out_port_between(self, src_dpid: int, dst_dpid: int) -> Optional[int]:
        """Return the port on src_dpid that leads directly to dst_dpid."""
        with self._lock:
            return self._sw_port.get((src_dpid, dst_dpid))

    def get_shortest_path(self, src_dpid: int, dst_dpid: int) -> list:
        """Return list of dpids for shortest weighted path, or []."""
        with self._lock:
            try:
                return nx.shortest_path(
                    self._graph, src_dpid, dst_dpid, weight="weight"
                )
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return []

    def get_out_port_toward(self, src_dpid: int, dst_dpid: int) -> Optional[int]:
        """
        Return the port on src_dpid toward dst_dpid.
        Checks direct link first; falls back to shortest-path next-hop.
        """
        with self._lock:
            direct = self._sw_port.get((src_dpid, dst_dpid))
            if direct is not None:
                return direct
            try:
                path = nx.shortest_path(
                    self._graph, src_dpid, dst_dpid, weight="weight"
                )
                if len(path) >= 2:
                    return self._sw_port.get((path[0], path[1]))
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                pass
        return None

    # ── Candidate path computation ────────────────────────────────────────────

    def get_candidate_paths(self, src_ip: str, dst_ip: str, host_manager) -> list:
        """
        Return up to MAX_CANDIDATE_PATHS simple paths (lists of dpids)
        between the switches that host src_ip and dst_ip.
        """
        cache_key = (src_ip, dst_ip)
        with self._lock:
            if cache_key in self._path_cache:
                return self._path_cache[cache_key]

        src_loc = host_manager.get_location(src_ip)
        dst_loc = host_manager.get_location(dst_ip)
        if src_loc is None or dst_loc is None:
            return []

        src_dpid, _ = src_loc
        dst_dpid, _ = dst_loc

        with self._lock:
            if src_dpid == dst_dpid:
                paths = [[src_dpid]]
                self._path_cache[cache_key] = paths
                return paths

            try:
                gen = nx.shortest_simple_paths(
                    self._graph, src_dpid, dst_dpid, weight="weight"
                )
                paths = []
                for p in gen:
                    paths.append(p)
                    if len(paths) >= MAX_CANDIDATE_PATHS:
                        break
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                paths = []

            self._path_cache[cache_key] = paths
            return paths

    def invalidate_path_cache(self, ip: str = None) -> None:
        """Clear all or IP-related cached paths."""
        with self._lock:
            if ip is None:
                self._path_cache.clear()
            else:
                stale = [k for k in self._path_cache if ip in k]
                for k in stale:
                    del self._path_cache[k]

    def get_congested_links(self, threshold: float) -> list:
        """Return list of (src_dpid, dst_dpid) pairs whose utilisation >= threshold."""
        with self._lock:
            return [
                (src, dst)
                for (src, dst), info in self._link_util.items()
                if info.get("util", 0.0) >= threshold
            ]

    def invalidate_cache_for_congested_paths(self, threshold: float) -> int:
        """
        Remove cached paths that route through any link whose utilisation >= threshold.
        Returns the number of cache entries cleared.
        Called after every monitoring cycle to ensure path selection uses fresh weights.
        """
        congested = set(self.get_congested_links(threshold))
        if not congested:
            return 0

        with self._lock:
            stale_keys = []
            for cache_key, paths in self._path_cache.items():
                for path in paths:
                    uses_congested = any(
                        (path[i], path[i + 1]) in congested
                        or (path[i + 1], path[i]) in congested
                        for i in range(len(path) - 1)
                    )
                    if uses_congested:
                        stale_keys.append(cache_key)
                        break
            for k in stale_keys:
                del self._path_cache[k]
            return len(stale_keys)

    # ── Link utilisation ──────────────────────────────────────────────────────

    def update_link_util(
        self, src_dpid: int, dst_dpid: int, rate_mbps: float, capacity_mbps: float
    ) -> None:
        util = min(rate_mbps / max(capacity_mbps, 0.001), 1.0)
        with self._lock:
            self._link_util[(src_dpid, dst_dpid)] = {
                "rate_mbps": round(rate_mbps, 4),
                "util": round(util, 4),
                "capacity_mbps": capacity_mbps,
            }
            if self._graph.has_edge(src_dpid, dst_dpid):
                self._graph[src_dpid][dst_dpid]["weight"] = max(0.01, util)

    def get_link_util(self, src_dpid: int, dst_dpid: int) -> dict:
        with self._lock:
            return dict(self._link_util.get((src_dpid, dst_dpid), {}))

    def path_max_util(self, path: list) -> float:
        """Return the bottleneck (max) utilisation along path (list of dpids)."""
        max_util = 0.0
        with self._lock:
            for i in range(len(path) - 1):
                info = self._link_util.get((path[i], path[i + 1]), {})
                max_util = max(max_util, info.get("util", 0.0))
        return max_util

    # ── State export helpers ──────────────────────────────────────────────────

    def get_all_links_for_state(self) -> list:
        """Return a deduplicated list of link dicts for state.json."""
        links = []
        with self._lock:
            seen = set()
            for (src, dst), port in self._sw_port.items():
                if (dst, src) in seen:
                    continue
                seen.add((src, dst))
                rev_port = self._sw_port.get((dst, src), 0)
                util_info = self._link_util.get((src, dst), {})
                links.append(
                    {
                        "src": f"s{src}",
                        "dst": f"s{dst}",
                        "src_port": port,
                        "dst_port": rev_port,
                        "rate_mbps": util_info.get("rate_mbps", 0.0),
                        "util": util_info.get("util", 0.0),
                        "capacity_mbps": util_info.get(
                            "capacity_mbps", DEFAULT_LINK_BW_MBPS
                        ),
                    }
                )
        return links

    def get_all_switch_dpids(self) -> list:
        with self._lock:
            return list(self._graph.nodes)

    def get_sw_port_map(self) -> dict:
        with self._lock:
            return dict(self._sw_port)

    @staticmethod
    def path_uses_link(path: list, a: int, b: int) -> bool:
        for i in range(len(path) - 1):
            if (path[i] == a and path[i + 1] == b) or (
                path[i] == b and path[i + 1] == a
            ):
                return True
        return False
