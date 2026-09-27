"""
utils/ip_utils.py — IP address classification helpers.
"""

from sdn.constants import MININET_SUBNET


def is_mininet_ip(ip: str) -> bool:
    """True if ip is in the configured Mininet subnet (default 10.0.0.x)."""
    return ip.startswith(MININET_SUBNET)


def is_valid_host_ip(ip: str) -> bool:
    """
    True if ip is a unicast address suitable for host learning:
      - in the Mininet subnet
      - not 0.0.0.0
      - not broadcast (.255)
      - not multicast
      - not link-local
    """
    if not ip or ip == "0.0.0.0":
        return False
    if is_multicast_ip(ip):
        return False
    if is_link_local_ip(ip):
        return False
    if ip.endswith(".255") or ip == "255.255.255.255":
        return False
    return is_mininet_ip(ip)


def is_multicast_ip(ip: str) -> bool:
    """True for 224.0.0.0/4 multicast addresses."""
    try:
        first = int(ip.split(".")[0])
        return 224 <= first <= 239
    except (ValueError, IndexError):
        return False


def is_link_local_ip(ip: str) -> bool:
    """True for 169.254.x.x link-local addresses."""
    return ip.startswith("169.254.")
