"""
sdn/flow_manager.py — OpenFlow rule installation, deletion, and PacketOut.

All public methods accept a datapath object or look one up via topology_manager.
Matches use both ipv4_src and ipv4_dst to avoid over-broad deletions.

Multipath support
-----------------
install_multipath_flows() installs an OF1.3 SELECT group on every switch
along all candidate paths.  Each bucket points to one next-hop port and
carries a weight proportional to the remaining capacity on that path.
The flow rule then sends matching packets to the group rather than a port.
"""

import logging

from sdn.constants import (
    ETH_TYPE_IP,
    FLOW_HARD_TIMEOUT,
    FLOW_IDLE_TIMEOUT,
    PRIORITY_FLOW as FLOW_PRIORITY,
    PRIORITY_TABLE_MISS,
)

_log = logging.getLogger(__name__)


# Group-id namespace: pack src/dst flow key into a deterministic integer.
# We use the lower 16 bits of hash so it stays within OF1.3 group_id range.
def _group_id_for(src_ip: str, dst_ip: str) -> int:
    return abs(hash((src_ip, dst_ip))) & 0xFFFF or 1


# ── Low-level OF helpers ──────────────────────────────────────────────────────


def install_table_miss(dp) -> None:
    """Install the table-miss entry: send all unknown packets to controller."""
    ofproto = dp.ofproto
    parser = dp.ofproto_parser
    match = parser.OFPMatch()
    actions = [
        parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)
    ]
    _add_flow(dp, PRIORITY_TABLE_MISS, match, actions, idle_timeout=0, hard_timeout=0)


def _add_flow(
    dp,
    priority: int,
    match,
    actions,
    idle_timeout: int = FLOW_IDLE_TIMEOUT,
    hard_timeout: int = FLOW_HARD_TIMEOUT,
) -> None:
    ofproto = dp.ofproto
    parser = dp.ofproto_parser
    inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
    dp.send_msg(
        parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )
    )


def delete_flow(dp, src_ip: str, dst_ip: str, priority: int = FLOW_PRIORITY) -> None:
    """
    Delete the flow rule that matches eth_type=IPv4, ipv4_src, ipv4_dst.
    Matching both src and dst prevents deleting unrelated flows.
    """
    ofproto = dp.ofproto
    parser = dp.ofproto_parser
    match = parser.OFPMatch(
        eth_type=ETH_TYPE_IP,
        ipv4_src=src_ip,
        ipv4_dst=dst_ip,
    )
    dp.send_msg(
        parser.OFPFlowMod(
            datapath=dp,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            priority=priority,
            match=match,
        )
    )


def delete_group(dp, group_id: int) -> None:
    """Delete a group table entry (no-op if it doesn't exist)."""
    ofproto = dp.ofproto
    parser = dp.ofproto_parser
    dp.send_msg(
        parser.OFPGroupMod(
            datapath=dp,
            command=ofproto.OFPGC_DELETE,
            type_=ofproto.OFPGT_SELECT,
            group_id=group_id,
        )
    )


def packet_out(dp, in_port: int, actions, data: bytes) -> None:
    """Send a PacketOut carrying raw packet bytes."""
    ofproto = dp.ofproto
    parser = dp.ofproto_parser
    dp.send_msg(
        parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=data,
        )
    )


# ── Single-path flow installation ─────────────────────────────────────────────


def install_path_flow(
    dp,
    src_ip: str,
    dst_ip: str,
    out_port: int,
) -> None:
    """
    Install one IPv4 forwarding rule on a single switch:
      match: eth_type=0x0800, ipv4_src=src_ip, ipv4_dst=dst_ip
      action: output(out_port)

    Deletes any stale rule for the same (src_ip, dst_ip) pair first.
    """
    parser = dp.ofproto_parser
    delete_flow(dp, src_ip, dst_ip, priority=FLOW_PRIORITY)
    match = parser.OFPMatch(
        eth_type=ETH_TYPE_IP,
        ipv4_src=src_ip,
        ipv4_dst=dst_ip,
    )
    _add_flow(
        dp,
        FLOW_PRIORITY,
        match,
        [parser.OFPActionOutput(out_port)],
        idle_timeout=FLOW_IDLE_TIMEOUT,
    )


def install_bidirectional_flows(
    src_ip: str,
    dst_ip: str,
    fwd_path: list,  # dpid list src -> dst
    rev_path: list,  # dpid list dst -> src
    host_manager,
    topology_manager,
    logger=None,
) -> None:
    """
    Install forwarding rules in both directions along the given paths.

    fwd_path: path for src_ip -> dst_ip
    rev_path: path for dst_ip -> src_ip
    """
    _install_one_direction(
        src_ip, dst_ip, fwd_path, host_manager, topology_manager, logger
    )
    _install_one_direction(
        dst_ip, src_ip, rev_path, host_manager, topology_manager, logger
    )


def _install_one_direction(
    src_ip: str,
    dst_ip: str,
    path: list,
    host_manager,
    topology_manager,
    logger=None,
) -> None:
    """
    Walk every switch in `path` and install the forwarding rule for src->dst.
    """
    dst_loc = host_manager.get_location(dst_ip)
    if dst_loc is None:
        return
    dst_dpid, dst_port = dst_loc

    for i, dpid in enumerate(path):
        dp = topology_manager.get_datapath(dpid)
        if dp is None:
            continue

        if i + 1 < len(path):
            next_dpid = path[i + 1]
            out_port = topology_manager.get_out_port_between(dpid, next_dpid)
        else:
            out_port = dst_port if dpid == dst_dpid else None

        if out_port is None:
            if logger:
                logger.warning(
                    "install_one_direction: no out_port at s%d for %s->%s",
                    dpid,
                    src_ip,
                    dst_ip,
                )
            continue

        install_path_flow(dp, src_ip, dst_ip, out_port)

        if logger:
            logger.info(
                "Flow installed: s%d  %s -> %s  out_port=%d",
                dpid,
                src_ip,
                dst_ip,
                out_port,
            )

    if logger:
        logger.info(
            "Path flow complete: %s -> %s  via [%s]",
            src_ip,
            dst_ip,
            " -> ".join(f"s{d}" for d in path),
        )


# ── Multipath (SELECT group) flow installation ────────────────────────────────


def install_multipath_flows(
    src_ip: str,
    dst_ip: str,
    fwd_paths: list,  # [[dpid,...], ...]  all candidate paths fwd
    fwd_weights: list,  # [int, ...]         weight per fwd path (same len)
    rev_paths: list,
    rev_weights: list,
    host_manager,
    topology_manager,
    logger=None,
) -> None:
    """
    Install OF1.3 SELECT groups and matching flow rules in both directions.

    For each switch that appears in *any* candidate path:
      1. Build one bucket per path that traverses this switch, weighted by
         the path's capacity share.
      2. MODIFY (or ADD) an OFPGT_SELECT group with those buckets.
      3. Install / update the flow rule to send to the group instead of a port.

    Switches that sit on only one path fall back to a plain output action
    (no group overhead needed, and SELECT with a single bucket is wasteful).
    """
    _install_one_direction_multipath(
        src_ip,
        dst_ip,
        fwd_paths,
        fwd_weights,
        host_manager,
        topology_manager,
        logger,
    )
    _install_one_direction_multipath(
        dst_ip,
        src_ip,
        rev_paths,
        rev_weights,
        host_manager,
        topology_manager,
        logger,
    )


def _install_one_direction_multipath(
    src_ip: str,
    dst_ip: str,
    paths: list,
    weights: list,
    host_manager,
    topology_manager,
    logger=None,
) -> None:
    """
    Core of install_multipath_flows for one direction.

    Algorithm
    ---------
    For each switch dpid that appears in at least one path:
      - Collect (out_port, weight) for each path that passes through dpid.
        * If dpid is not the last hop: out_port = port toward next switch.
        * If dpid is the last hop and hosts dst_ip: out_port = host port.
        * Otherwise: skip this path at this switch.
      - Deduplicate by port (sum weights for paths sharing a port).
      - If only one unique port: install plain output flow.
      - If multiple ports: install SELECT group + group-action flow.
    """
    dst_loc = host_manager.get_location(dst_ip)
    if dst_loc is None:
        return
    dst_dpid, dst_port = dst_loc

    group_id = _group_id_for(src_ip, dst_ip)

    # Collect per-switch port->weight mapping across all paths
    # sw_buckets[dpid] = {out_port: accumulated_weight}
    sw_buckets: dict = {}

    for path, w in zip(paths, weights):
        for i, dpid in enumerate(path):
            if i + 1 < len(path):
                next_dpid = path[i + 1]
                out_port = topology_manager.get_out_port_between(dpid, next_dpid)
            elif dpid == dst_dpid:
                out_port = dst_port
            else:
                out_port = None

            if out_port is None:
                continue

            if dpid not in sw_buckets:
                sw_buckets[dpid] = {}
            sw_buckets[dpid][out_port] = sw_buckets[dpid].get(out_port, 0) + w

    # Install rules on each switch
    for dpid, port_weights in sw_buckets.items():
        dp = topology_manager.get_datapath(dpid)
        if dp is None:
            continue

        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        delete_flow(dp, src_ip, dst_ip, priority=FLOW_PRIORITY)

        if len(port_weights) == 1:
            # Only one egress port at this switch — plain output, no group needed
            out_port = next(iter(port_weights))
            match = parser.OFPMatch(
                eth_type=ETH_TYPE_IP,
                ipv4_src=src_ip,
                ipv4_dst=dst_ip,
            )
            _add_flow(
                dp,
                FLOW_PRIORITY,
                match,
                [parser.OFPActionOutput(out_port)],
                idle_timeout=FLOW_IDLE_TIMEOUT,
            )
            if logger:
                logger.info(
                    "Multipath single-port: s%d  %s->%s  out=%d",
                    dpid,
                    src_ip,
                    dst_ip,
                    out_port,
                )
        else:
            # Multiple egress ports — install SELECT group
            buckets = []
            for out_port, bkt_weight in port_weights.items():
                bucket_actions = [parser.OFPActionOutput(out_port)]
                buckets.append(
                    parser.OFPBucket(
                        weight=int(bkt_weight),
                        watch_port=out_port,
                        watch_group=ofproto.OFPG_ANY,
                        actions=bucket_actions,
                    )
                )

            # Try MODIFY first; if it fails OVS will return an error and we ADD
            dp.send_msg(
                parser.OFPGroupMod(
                    datapath=dp,
                    command=ofproto.OFPGC_MODIFY,
                    type_=ofproto.OFPGT_SELECT,
                    group_id=group_id,
                    buckets=buckets,
                )
            )
            # Always send an ADD as well — OVS silently ignores if already present
            dp.send_msg(
                parser.OFPGroupMod(
                    datapath=dp,
                    command=ofproto.OFPGC_ADD,
                    type_=ofproto.OFPGT_SELECT,
                    group_id=group_id,
                    buckets=buckets,
                )
            )

            match = parser.OFPMatch(
                eth_type=ETH_TYPE_IP,
                ipv4_src=src_ip,
                ipv4_dst=dst_ip,
            )
            _add_flow(
                dp,
                FLOW_PRIORITY,
                match,
                [parser.OFPActionGroup(group_id=group_id)],
                idle_timeout=FLOW_IDLE_TIMEOUT,
            )

            if logger:
                bucket_summary = ", ".join(
                    f"port{p}×{w}" for p, w in port_weights.items()
                )
                logger.info(
                    "Multipath SELECT group: s%d  %s->%s  group=%d  [%s]",
                    dpid,
                    src_ip,
                    dst_ip,
                    group_id,
                    bucket_summary,
                )
