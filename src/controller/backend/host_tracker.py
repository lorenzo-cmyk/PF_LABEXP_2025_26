"""HostTracker — thread-safe IP-MAC-Switch-Port binding table.

MAC→location is populated from os-ken topology events
(EventHostAdd / EventHostMove).  IP→MAC mappings are populated
from ARP packet inspection in the controller's packet-in handler.

os-ken runs its own host discovery before our app sees the packet,
so MAC locations are always up-to-date when we consume them.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostLocation:
    dpid: int
    port: int


@dataclass
class HostEntry:
    location: HostLocation
    ips: set[str] = field(default_factory=set)


class HostTracker:
    """Thread-safe host binding table.

    MAC locations are fed by the Backend when os-ken fires
    EventHostAdd / EventHostMove.  IP addresses are fed by the
    packet-in handler when it sees ARP / IPv4 packets.
    """

    def __init__(self) -> None:
        self._table: dict[str, HostEntry] = {}
        self._lock = threading.Lock()

    # ── Mutators (called from os-ken event loop) ──────────────────────

    def add_host(self, mac: str, dpid: int, port: int) -> Optional[HostLocation]:
        """Record a host location (from os-ken EventHostAdd / EventHostMove).

        Returns the previous location if the host moved, or None.
        """
        with self._lock:
            loc = HostLocation(dpid, port)
            entry = self._table.get(mac)
            if entry is None:
                self._table[mac] = HostEntry(location=loc)
                LOG.info(
                    "HostTracker: added %s → dpid=%s port=%d (total=%d)",
                    mac,
                    hex(dpid),
                    port,
                    len(self._table),
                )
                return None
            prev = entry.location
            if prev == loc:
                return None
            entry.location = loc
            LOG.info(
                "HostTracker: %s MOVED dpid=%s:%d → dpid=%s:%d",
                mac,
                hex(prev.dpid),
                prev.port,
                hex(dpid),
                port,
            )
            return prev

    def add_ip(self, mac: str, ip: str) -> None:
        """Associate an IPv4 address with *mac* (from observed traffic)."""
        with self._lock:
            entry = self._table.get(mac)
            if entry is None:
                LOG.debug(
                    "HostTracker: add_ip %s for unknown MAC %s — ignored", ip, mac
                )
                return
            if ip not in entry.ips:
                entry.ips.add(ip)
                LOG.info(
                    "HostTracker: %s now has IP %s (total=%d)", mac, ip, len(entry.ips)
                )

    def remove_by_port(self, dpid: int, port: int) -> list[str]:
        """Remove all hosts on *(dpid, port)*. Returns the removed MACs."""
        removed: list[str] = []
        with self._lock:
            for mac, entry in list(self._table.items()):
                if entry.location == HostLocation(dpid, port):
                    del self._table[mac]
                    removed.append(mac)
        if removed:
            LOG.info(
                "HostTracker: removed %d hosts on dpid=%s port=%d: %s",
                len(removed),
                hex(dpid),
                port,
                ", ".join(removed),
            )
        return removed

    def remove_mac(self, mac: str) -> Optional[HostEntry]:
        """Remove a single host entry."""
        with self._lock:
            return self._table.pop(mac, None)

    # ── Queries (safe from any thread) ────────────────────────────────

    def lookup(self, mac: str) -> Optional[HostLocation]:
        """Return the location of *mac*, or None."""
        with self._lock:
            entry = self._table.get(mac)
        if entry:
            return entry.location
        return None

    def lookup_by_ip(self, ip: str) -> Optional[tuple[str, int, int]]:
        """Return (mac, dpid, port) for the host owning *ip*, or None."""
        with self._lock:
            for mac, entry in self._table.items():
                if ip in entry.ips:
                    return (mac, entry.location.dpid, entry.location.port)
        return None

    def get_all_hosts(self) -> list[dict]:
        """Return all known hosts with IP, MAC, and location — for the REST API."""
        with self._lock:
            return [
                {
                    "mac": mac,
                    "ips": sorted(entry.ips),
                    "dpid": entry.location.dpid,
                    "port": entry.location.port,
                }
                for mac, entry in self._table.items()
            ]

    @property
    def hosts(self) -> dict[str, HostLocation]:
        """Return a snapshot of all MAC→location mappings."""
        with self._lock:
            return {mac: entry.location for mac, entry in self._table.items()}
