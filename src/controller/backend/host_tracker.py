"""HostTracker — learns MAC → (dpid, port) from packet-in events."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from topology import TopologyGraph

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostLocation:
    dpid: int
    port: int


class HostTracker:
    """Maps MAC addresses to their most recently observed switch + port."""

    def __init__(self, graph: Optional[TopologyGraph] = None) -> None:
        self._table: dict[str, HostLocation] = {}
        self._lock = threading.Lock()
        self._graph = graph

    def learn(self, mac: str, dpid: int, port: int) -> Optional[HostLocation]:
        """Learn a host location.

        Returns the *previous* location if the host moved (so the caller can
        purge stale flows), or None if the host was brand-new or unchanged.
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
                    return None
                if not self._is_edge_port(dpid, port):
                    LOG.warning(
                        "HostTracker: %s seen at dpid=%s port=%d (internal) "
                        "but already known at dpid=%s port=%d — keeping original",
                        mac,
                        hex(dpid),
                        port,
                        hex(prev.dpid),
                        prev.port,
                    )
                    return None
                LOG.info(
                    "HostTracker: %s MOVED from dpid=%s port=%d → dpid=%s port=%d",
                    mac,
                    hex(prev.dpid),
                    prev.port,
                    hex(dpid),
                    port,
                )
                old_loc = prev
            else:
                if not self._is_edge_port(dpid, port):
                    LOG.warning(
                        "HostTracker: ignoring %s at dpid=%s port=%d (internal, new)",
                        mac,
                        hex(dpid),
                        port,
                    )
                    return None
                old_loc = None
            self._table[mac] = HostLocation(dpid, port)
        LOG.info(
            "HostTracker: learned %s → dpid=%s port=%d (total=%d)",
            mac,
            hex(dpid),
            port,
            len(self._table),
        )
        return old_loc

    def lookup(self, mac: str) -> Optional[HostLocation]:
        """Return the most recently observed location of *mac*, or None."""
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
        """Remove all hosts learned on *(dpid, port)*. Returns the removed MACs."""
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
        """Return a snapshot of all known MAC→location mappings."""
        with self._lock:
            return dict(self._table)

    def _is_edge_port(self, dpid: int, port: int) -> bool:
        """True if (dpid, port) is a known edge (host-facing) port.

        If no graph reference is available, conservatively returns True
        (allows mobility without the safety check).
        """
        if self._graph is None:
            return True
        return (dpid, port) in self._graph.edge_ports
