"""HostTracker — learns MAC → (dpid, port) from packet-in events."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostLocation:
    dpid: int
    port: int


class HostTracker:
    """Maps MAC addresses to their most recently observed switch + port."""

    def __init__(self) -> None:
        self._table: dict[str, HostLocation] = {}
        self._lock = threading.Lock()

    def learn(self, mac: str, dpid: int, port: int) -> bool:
        """Learn a host location. Returns True if a new location was recorded.

        Once a host is learned, its location is NOT updated (no mobility in GOAL 1).
        This prevents broadcast storms from corrupting the host tracker with
        wrong locations (packets looping through internal ports).
        """
        with self._lock:
            prev = self._table.get(mac)
            if prev is not None:
                if prev.dpid == dpid and prev.port == port:
                    LOG.debug(
                        "HostTracker: refreshed %s → dpid=%s port=%d",
                        mac,
                        hex(dpid),
                        port,
                    )
                else:
                    LOG.warning(
                        "HostTracker: %s seen at dpid=%s port=%d but already known at "
                        "dpid=%s port=%d — keeping original (GOAL 1: no mobility)",
                        mac,
                        hex(dpid),
                        port,
                        hex(prev.dpid),
                        prev.port,
                    )
                return False
            self._table[mac] = HostLocation(dpid, port)
        LOG.info(
            "HostTracker: learned %s → dpid=%s port=%d (new host | total=%d)",
            mac,
            hex(dpid),
            port,
            len(self._table),
        )
        return True

    def lookup(self, mac: str) -> Optional[HostLocation]:
        with self._lock:
            loc = self._table.get(mac)
        if loc:
            LOG.debug(
                "HostTracker: lookup %s → dpid=%s port=%d", mac, hex(loc.dpid), loc.port
            )
        else:
            LOG.debug("HostTracker: lookup %s → UNKNOWN", mac)
        return loc

    def remove_by_port(self, dpid: int, port: int) -> list[str]:
        removed: list[str] = []
        with self._lock:
            for mac, loc in list(self._table.items()):
                if loc.dpid == dpid and loc.port == port:
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

    @property
    def hosts(self) -> dict[str, HostLocation]:
        with self._lock:
            return dict(self._table)
