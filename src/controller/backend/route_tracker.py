"""RouteTracker — tracks which (src_mac, dst_mac) pairs use each link.

Used during fault recovery to identify exactly which flows need deletion
when a link goes down.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict

from topology import LinkKey

LOG = logging.getLogger(__name__)


class RouteTracker:
    """Maps Link → set of (src_mac, dst_mac) pairs, and vice versa."""

    def __init__(self) -> None:
        self._link_to_pairs: dict[tuple, set[tuple[str, str]]] = defaultdict(set)
        self._pair_to_links: dict[tuple[str, str], list[LinkKey]] = {}
        self._lock = threading.Lock()

    def add_route(self, src_mac: str, dst_mac: str, path_links: list[LinkKey]) -> None:
        """Record that traffic for (src_mac, dst_mac) uses the given links."""
        with self._lock:
            pair = (src_mac, dst_mac)
            old_count = len(self._pair_to_links.get(pair, []))
            self._remove_pair_unsafe(pair)

            self._pair_to_links[pair] = list(path_links)
            for lk in path_links:
                self._link_to_pairs[lk.undirected_key].add(pair)

        link_str = ", ".join(
            f"{hex(lk.src_dpid)}:{lk.src_port}→{hex(lk.dst_dpid)}:{lk.dst_port}"
            for lk in path_links
        )
        LOG.info(
            "RouteTracker: added route %s → %s | links=[%s] (replaced %d old links)",
            src_mac,
            dst_mac,
            link_str,
            old_count,
        )

    def remove_route(self, src_mac: str, dst_mac: str) -> None:
        """Remove all link tracking for a (src_mac, dst_mac) pair.

        Called during fault recovery (link down) and during host moves
        to clear the stale route before a new path is computed.
        """
        with self._lock:
            pair = (src_mac, dst_mac)
            links = self._pair_to_links.get(pair, [])
            self._remove_pair_unsafe(pair)
        if links:
            LOG.info(
                "RouteTracker: removed route %s → %s (%d links)",
                src_mac,
                dst_mac,
                len(links),
            )
        else:
            LOG.debug(
                "RouteTracker: remove_route %s → %s (not tracked)", src_mac, dst_mac
            )

    def _remove_pair_unsafe(self, pair: tuple[str, str]) -> None:
        """Remove all link indices for *pair* — caller must hold ``self._lock``.

        Pops the pair from ``_pair_to_links``, then removes it from every
        link's reverse-index set.  Cleans up empty link-index entries to
        prevent unbounded growth.
        """
        old_links = self._pair_to_links.pop(pair, [])
        for lk in old_links:
            self._link_to_pairs[lk.undirected_key].discard(pair)
            if not self._link_to_pairs[lk.undirected_key]:
                del self._link_to_pairs[lk.undirected_key]

    def pairs_on_link(self, link: LinkKey) -> set[tuple[str, str]]:
        """Return all (src_mac, dst_mac) pairs affected by a link failure."""
        with self._lock:
            pairs = set(self._link_to_pairs.get(link.undirected_key, set()))
        LOG.info(
            "RouteTracker: pairs on link %s:%d→%s:%d = %d",
            hex(link.src_dpid),
            link.src_port,
            hex(link.dst_dpid),
            link.dst_port,
            len(pairs),
        )
        for src, dst in pairs:
            LOG.info("RouteTracker:   affected pair: %s → %s", src, dst)
        return pairs

    def links_for_pair(self, src_mac: str, dst_mac: str) -> list[LinkKey]:
        """Return the list of ``LinkKey`` edges currently used by the pair.

        Returns an empty list when the pair is not tracked (no route
        has been installed yet or the route was purged).
        """
        with self._lock:
            links = list(self._pair_to_links.get((src_mac, dst_mac), []))
        LOG.debug("RouteTracker: links for %s → %s = %d", src_mac, dst_mac, len(links))
        return links

    @property
    def all_routes(self) -> dict[tuple[str, str], list[LinkKey]]:
        """Return a snapshot of all tracked (pair → links) mappings.

        Used by the REST API (``GET /flows``) and during fault recovery
        to enumerate all routes that may be affected by a topology change.
        Each pair's link list is a copy; the internal dict is not exposed.
        """
        with self._lock:
            return {pair: list(links) for pair, links in self._pair_to_links.items()}

    def clear(self) -> None:
        """Remove all tracked routes (used on full topology reset)."""
        with self._lock:
            pair_count = len(self._pair_to_links)
            self._link_to_pairs.clear()
            self._pair_to_links.clear()
        LOG.info("RouteTracker: cleared all routes (%d pairs removed)", pair_count)

    def purge_switch(self, dpid: int) -> list[tuple[str, str]]:
        """Remove all routes that pass through a given switch (e.g., it powered off).

        Returns the list of (src_mac, dst_mac) pairs that were purged.
        """
        purged: list[tuple[str, str]] = []
        with self._lock:
            for pair, links in list(self._pair_to_links.items()):
                for lk in links:
                    if lk.src_dpid == dpid or lk.dst_dpid == dpid:
                        purged.append(pair)
                        break  # no need to check remaining links
            for pair in purged:
                self._remove_pair_unsafe(pair)
        if purged:
            LOG.info(
                "RouteTracker: purged %d routes involving dead switch dpid=%s",
                len(purged),
                hex(dpid),
            )
        return purged

    def purge_mac(self, mac: str) -> list[tuple[str, str]]:
        """Remove all routes involving a given MAC (e.g., host disconnected).

        Returns the list of (src_mac, dst_mac) pairs that were purged.
        """
        purged: list[tuple[str, str]] = []
        with self._lock:
            for pair in list(self._pair_to_links.keys()):
                if pair[0] == mac or pair[1] == mac:
                    purged.append(pair)
            for pair in purged:
                self._remove_pair_unsafe(pair)
        if purged:
            LOG.info(
                "RouteTracker: purged %d routes involving disconnected host %s",
                len(purged),
                mac,
            )
        return purged
