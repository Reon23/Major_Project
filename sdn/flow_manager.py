"""
sdn/flow_manager.py — OpenFlow rule installation, deletion, and PacketOut.

All public methods accept a datapath object or look one up via topology_manager.
Matches use both ipv4_src and ipv4_dst to avoid over-broad deletions.
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


# ── Low-level OF helpers ──────────────────────────────────────────────────────

def install_table_miss(dp) -> None:
    """Install the table-miss entry: send all unknown packets to controller."""
    ofproto = dp.ofproto
    parser = dp.ofproto_parser
    match = parser.OFPMatch()
    actions = [parser.OFPActionOutput(
        ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER
    )]
    _add_flow(dp, PRIORITY_TABLE_MISS, match, actions,
              idle_timeout=0, hard_timeout=0)


def _add_flow(dp, priority: int, match, actions,
              idle_timeout: int = FLOW_IDLE_TIMEOUT,
              hard_timeout: int = FLOW_HARD_TIMEOUT) -> None:
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


def delete_flow(dp, src_ip: str, dst_ip: str,
                priority: int = FLOW_PRIORITY) -> None:
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


# ── Path-level flow installation ─────────────────────────────────────────────

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
    # Remove stale rule before installing the fresh one
    delete_flow(dp, src_ip, dst_ip, priority=FLOW_PRIORITY)
    match = parser.OFPMatch(
        eth_type=ETH_TYPE_IP,
        ipv4_src=src_ip,
        ipv4_dst=dst_ip,
    )
    _add_flow(
        dp, FLOW_PRIORITY, match,
        [parser.OFPActionOutput(out_port)],
        idle_timeout=FLOW_IDLE_TIMEOUT,
    )


def install_bidirectional_flows(
    src_ip: str,
    dst_ip: str,
    fwd_path: list,    # dpid list src -> dst
    rev_path: list,    # dpid list dst -> src
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

        # Determine output port
        if i + 1 < len(path):
            next_dpid = path[i + 1]
            out_port = topology_manager.get_out_port_between(dpid, next_dpid)
        else:
            # Last (or only) switch: deliver to host port
            out_port = dst_port if dpid == dst_dpid else None

        if out_port is None:
            if logger:
                logger.warning(
                    "install_one_direction: no out_port at s%d for %s->%s",
                    dpid, src_ip, dst_ip,
                )
            continue

        install_path_flow(dp, src_ip, dst_ip, out_port)

        if logger:
            logger.info(
                "Flow installed: s%d  %s -> %s  out_port=%d",
                dpid, src_ip, dst_ip, out_port,
            )

    if logger:
        logger.info(
            "Path flow complete: %s -> %s  via [%s]",
            src_ip, dst_ip,
            " -> ".join(f"s{d}" for d in path),
        )
