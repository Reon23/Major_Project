"""
sdn/host_manager.py — Host learning, IP/MAC/location tables.

Rejects learning from:
  - trunk / inter-switch ports
  - IPs outside the Mininet subnet
  - 0.0.0.0, multicast, link-local, broadcast
"""

import threading
from typing import Optional, Tuple

from utils.ip_utils import is_valid_host_ip


class HostManager:
    """
    Maintains:
      ip_to_mac       : ip  -> mac
      mac_to_ip       : mac -> ip
      host_location   : ip  -> (dpid, port)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._ip_to_mac = {}
        self._mac_to_ip = {}
        self._host_location = {}

    def learn_host(
        self,
        ip: str,
        mac: str,
        dpid: int,
        port: int,
        topology_manager,
        logger=None,
    ) -> bool:
        """
        Attempt to learn/update a host.

        Returns True if the location was new or updated (caller should
        trigger flow re-installation), False otherwise.
        """
        # ── Reject invalid IPs ─────────────────────────────────────────────
        if not is_valid_host_ip(ip):
            return False

        # ── Reject learning from trunk / inter-switch ports ────────────────
        if topology_manager.is_trunk_port(dpid, port):
            if logger:
                logger.debug(
                    "Ignored trunk-port host learning: ip=%s mac=%s s%d-eth%d",
                    ip, mac, dpid, port,
                )
            return False

        with self._lock:
            old_mac = self._ip_to_mac.get(ip)
            old_loc = self._host_location.get(ip)

            changed = False

            if old_mac != mac:
                self._ip_to_mac[ip] = mac
                self._mac_to_ip[mac] = ip
                changed = True
                if logger:
                    logger.info(
                        "Learned host: ip=%s mac=%s s%d-eth%d", ip, mac, dpid, port
                    )

            if old_loc != (dpid, port):
                self._host_location[ip] = (dpid, port)
                changed = True
                if logger:
                    logger.info(
                        "Host location: ip=%s -> s%d port %d", ip, dpid, port
                    )

        return changed

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_location(self, ip: str) -> Optional[Tuple[int, int]]:
        with self._lock:
            return self._host_location.get(ip)

    def get_mac(self, ip: str) -> Optional[str]:
        with self._lock:
            return self._ip_to_mac.get(ip)

    def is_known(self, ip: str) -> bool:
        with self._lock:
            return ip in self._host_location

    def all_ips(self) -> list:
        with self._lock:
            return list(self._host_location.keys())

    def all_hosts_for_state(self) -> list:
        with self._lock:
            return [
                {"id": f"h_{ip}", "type": "host", "ip": ip}
                for ip in self._host_location
            ]

    def get_all_locations(self) -> dict:
        with self._lock:
            return dict(self._host_location)

    def invalidate(self, ip: str) -> None:
        """Remove all records for ip (e.g. when host location invalidated)."""
        with self._lock:
            mac = self._ip_to_mac.pop(ip, None)
            if mac:
                self._mac_to_ip.pop(mac, None)
            self._host_location.pop(ip, None)
