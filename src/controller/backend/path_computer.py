"""PathComputer — symmetric shortest-path computation on TopologyGraph."""

from __future__ import annotations

import logging
import threading
from typing import Optional

import networkx as nx

from topology import TopologyGraph

LOG = logging.getLogger(__name__)


class PathComputer:
    """Computes shortest paths and enforces symmetry.

    When computing A → B, the reverse B → A path is cached simultaneously
    so both directions always use the same links.
    """

    def __init__(self, graph: TopologyGraph) -> None:
        self.graph = graph
        self._cache: dict[tuple[int, int], list[int]] = {}
        self._lock = threading.Lock()

    def compute_path(self, src_dpid: int, dst_dpid: int) -> Optional[list[int]]:
        """Return list of dpids from src to dst (inclusive), or None if unreachable."""
        with self._lock:
            key = (src_dpid, dst_dpid)
            if key in self._cache:
                LOG.debug(
                    "Path: cache HIT %s → %s: %s",
                    hex(src_dpid),
                    hex(dst_dpid),
                    " → ".join(hex(d) for d in self._cache[key]),
                )
                return list(self._cache[key])

        g = self.graph.copy_graph()
        if src_dpid not in g:
            LOG.warning("Path: src %s not in graph", hex(src_dpid))
            return None
        if dst_dpid not in g:
            LOG.warning("Path: dst %s not in graph", hex(dst_dpid))
            return None

        try:
            path: list[int] = nx.shortest_path(g, src_dpid, dst_dpid)
        except nx.NetworkXNoPath:
            LOG.warning("Path: NO PATH %s → %s", hex(src_dpid), hex(dst_dpid))
            return None

        reverse_path = list(reversed(path))
        path_str = " → ".join(hex(d) for d in path)
        LOG.info(
            "Path: computed %s → %s: %s (cached both directions)",
            hex(src_dpid),
            hex(dst_dpid),
            path_str,
        )

        with self._lock:
            self._cache[(src_dpid, dst_dpid)] = path
            self._cache[(dst_dpid, src_dpid)] = reverse_path

        return path

    def invalidate(self) -> None:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
        LOG.info("Path: invalidated ALL cached paths (%d entries cleared)", count)

    def invalidate_pair(self, src_dpid: int, dst_dpid: int) -> None:
        with self._lock:
            self._cache.pop((src_dpid, dst_dpid), None)
            self._cache.pop((dst_dpid, src_dpid), None)
        LOG.debug("Path: invalidated pair %s ↔ %s", hex(src_dpid), hex(dst_dpid))
