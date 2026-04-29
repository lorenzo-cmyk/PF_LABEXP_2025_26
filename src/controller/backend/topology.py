"""TopologyGraph (pure NetworkX) + TopologyManager (os-ken LLDP discovery)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import networkx as nx

if TYPE_CHECKING:
    from os_ken.controller.controller import Datapath
    from os_ken.topology.switches import Link

LOG = logging.getLogger(__name__)


# ── Pure graph model (no os-ken dependencies) ──────────────────────────


@dataclass(frozen=True)
class LinkKey:
    """Immutable identifier for a directed link: src switch/port → dst switch/port."""

    src_dpid: int
    src_port: int
    dst_dpid: int
    dst_port: int

    @property
    def reverse(self) -> "LinkKey":
        return LinkKey(self.dst_dpid, self.dst_port, self.src_dpid, self.src_port)

    @property
    def undirected_key(self) -> tuple[int, int, int, int]:
        """Canonical key for the undirected link (smaller dpid first)."""
        if (self.src_dpid, self.src_port) <= (self.dst_dpid, self.dst_port):
            return (self.src_dpid, self.src_port, self.dst_dpid, self.dst_port)
        return (self.dst_dpid, self.dst_port, self.src_dpid, self.src_port)


class TopologyGraph:
    """Pure Python graph model of the network. Thread-safe via a single lock.

    Nodes: dpid (int)
    Edges: (dpid_a, dpid_b, key) with attributes storing both port numbers.
      Uses ``nx.MultiGraph`` so multiple parallel links per switch pair coexist.
    Edge ports (host-facing) are tracked as a set of ``(dpid, port_no)`` tuples.
    """

    def __init__(self) -> None:
        self._graph = nx.MultiGraph()
        self._lock = threading.RLock()
        self._edge_ports: set[tuple[int, int]] = set()
        self._switch_ports: dict[int, set[int]] = {}
        self._known_internal_ports: set[tuple[int, int]] = set()

    # -- graph mutations --------------------------------------------------

    def add_switch(self, dpid: int) -> None:
        """Register a switch node in the graph."""
        with self._lock:
            self._graph.add_node(dpid)
            self._switch_ports.setdefault(dpid, set())
        LOG.info(
            "Graph: added switch dpid=%s | switches=%d",
            hex(dpid),
            len(self._graph.nodes),
        )

    def remove_switch(self, dpid: int) -> None:
        """Remove a switch and all its ports, links, and edge markings."""
        with self._lock:
            self._graph.remove_node(dpid)
            self._switch_ports.pop(dpid, None)
            self._edge_ports = {ep for ep in self._edge_ports if ep[0] != dpid}
            self._known_internal_ports = {
                p for p in self._known_internal_ports if p[0] != dpid
            }
        LOG.info(
            "Graph: removed switch dpid=%s | switches=%d",
            hex(dpid),
            len(self._graph.nodes),
        )

    def add_port(self, dpid: int, port_no: int) -> None:
        """Register a port on a switch.

        If the port is not known to be an internal (switch-to-switch) link,
        it is assumed to be edge (host-facing).  Later, when LLDP discovers
        a link on this port, ``add_link()`` will remove the edge marking.
        """
        with self._lock:
            self._switch_ports.setdefault(dpid, set()).add(port_no)
            if (dpid, port_no) not in self._known_internal_ports:
                self._edge_ports.add((dpid, port_no))
                LOG.debug(
                    "Graph: added port dpid=%s port=%d (assumed edge)",
                    hex(dpid),
                    port_no,
                )
            else:
                LOG.debug(
                    "Graph: added port dpid=%s port=%d (known link, no edge)",
                    hex(dpid),
                    port_no,
                )

    def remove_port(self, dpid: int, port_no: int) -> None:
        """Remove a port from a switch and tear down any link involving it.

        Iterates all graph edges (now with keys) to find the one that uses
        *port_no* on *dpid*, then removes both the edge marking and the
        specific graph edge (by key, leaving other parallel links intact).
        """
        removed_links: list[str] = []
        with self._lock:
            self._switch_ports.get(dpid, set()).discard(port_no)
            self._edge_ports.discard((dpid, port_no))
            edges_to_remove = []
            for u, v, key, data in self._graph.edges(data=True, keys=True):
                port_u = data["port_a"] if data["dpid_a"] == u else data["port_b"]
                port_v = data["port_b"] if data["dpid_a"] == u else data["port_a"]
                if (u == dpid and port_u == port_no) or (
                    v == dpid and port_v == port_no
                ):
                    edges_to_remove.append((u, v, key))
            for u, v, key in edges_to_remove:
                self._graph.remove_edge(u, v, key=key)
                removed_links.append(f"{hex(u)}↔{hex(v)}")
        if removed_links:
            LOG.warning(
                "Graph: removed port dpid=%s port=%d → tore down links: %s",
                hex(dpid),
                port_no,
                ", ".join(removed_links),
            )
        else:
            LOG.debug(
                "Graph: removed port dpid=%s port=%d (edge port)", hex(dpid), port_no
            )

    def add_link(self, link: LinkKey) -> None:
        """Record a switch-to-switch link discovered by LLDP.

        Each link is keyed by its canonical undirected 4-tuple so that
        multiple parallel links between the same switch pair coexist.
        The port pair is removed from ``_edge_ports`` and recorded in
        ``_known_internal_ports`` to prevent future reclassification as edge.
        """
        with self._lock:
            a, b = min(link.src_dpid, link.dst_dpid), max(link.src_dpid, link.dst_dpid)
            port_a = link.src_port if link.src_dpid == a else link.dst_port
            port_b = link.dst_port if link.dst_dpid == b else link.src_port
            self._graph.add_edge(
                a,
                b,
                key=link.undirected_key,
                dpid_a=a,
                port_a=port_a,
                dpid_b=b,
                port_b=port_b,
            )
            self._edge_ports.discard((link.src_dpid, link.src_port))
            self._edge_ports.discard((link.dst_dpid, link.dst_port))
            self._known_internal_ports.add((link.src_dpid, link.src_port))
            self._known_internal_ports.add((link.dst_dpid, link.dst_port))
        LOG.info(
            "Graph: added link %s:%d → %s:%d | edges=%d",
            hex(link.src_dpid),
            link.src_port,
            hex(link.dst_dpid),
            link.dst_port,
            len(self._graph.edges),
        )

    def remove_link(self, link: LinkKey) -> None:
        """Remove a specific switch-to-switch link from the graph.

        Finds the edge whose port attributes match the given *link*, then
        removes only that edge (by key), leaving other parallel links intact.
        The ports are deliberately *not* reverted to edge status.
        """
        with self._lock:
            target_key = None
            for u, v, key, data in self._graph.edges(data=True, keys=True):
                if data["dpid_a"] not in (link.src_dpid, link.dst_dpid):
                    continue
                if data["dpid_b"] not in (link.src_dpid, link.dst_dpid):
                    continue
                port_u = data["port_a"] if data["dpid_a"] == u else data["port_b"]
                port_v = data["port_b"] if data["dpid_a"] == u else data["port_a"]
                if (
                    u == link.src_dpid
                    and port_u == link.src_port
                    and v == link.dst_dpid
                    and port_v == link.dst_port
                ):
                    target_key = key
                    break
                if (
                    v == link.src_dpid
                    and port_v == link.src_port
                    and u == link.dst_dpid
                    and port_u == link.dst_port
                ):
                    target_key = key
                    break
            if target_key is not None:
                self._graph.remove_edge(link.src_dpid, link.dst_dpid, key=target_key)
                removed = True
            else:
                removed = False
        if removed:
            LOG.info(
                "Graph: removed link %s:%d → %s:%d | edges=%d",
                hex(link.src_dpid),
                link.src_port,
                hex(link.dst_dpid),
                link.dst_port,
                len(self._graph.edges),
            )
        else:
            LOG.debug(
                "Graph: remove_link %s:%d → %s:%d (edge already absent)",
                hex(link.src_dpid),
                link.src_port,
                hex(link.dst_dpid),
                link.dst_port,
            )

    # -- read-only queries ------------------------------------------------

    @property
    def switches(self) -> list[int]:
        """Return all switch dpids currently in the graph (snapshot)."""
        with self._lock:
            return list(self._graph.nodes())

    @property
    def edge_ports(self) -> set[tuple[int, int]]:
        """Return all (dpid, port_no) pairs believed to be host-facing."""
        with self._lock:
            return set(self._edge_ports)

    @property
    def links(self) -> list[LinkKey]:
        """Return one canonical link per multi-edge (smaller dpid first)."""
        with self._lock:
            result = []
            for u, v, key, data in self._graph.edges(data=True, keys=True):
                a, b = data["dpid_a"], data["dpid_b"]
                port_a, port_b = data["port_a"], data["port_b"]
                result.append(LinkKey(a, port_a, b, port_b))
            return result

    def get_port_for_peer(self, src_dpid: int, dst_dpid: int) -> Optional[int]:
        """Return the port on *src_dpid* that connects to *dst_dpid*, or None.

        With MultiGraph there may be several parallel links; returns the port
        from the first edge found (insertion order).
        """
        with self._lock:
            if not self._graph.has_edge(src_dpid, dst_dpid):
                return None
            for key, data in self._graph[src_dpid][dst_dpid].items():
                if data["dpid_a"] == src_dpid:
                    return data["port_a"]
                return data["port_b"]
            return None

    def is_known_internal(self, dpid: int, port_no: int) -> bool:
        """Return True if *(dpid, port_no)* was ever part of a link (even if torn)."""
        with self._lock:
            return (dpid, port_no) in self._known_internal_ports

    def is_port_connected(self, dpid: int, port_no: int) -> bool:
        """Return True if *port_no* on *dpid* currently has an active graph edge."""
        with self._lock:
            for u, v, key, data in self._graph.edges(data=True, keys=True):
                if u == dpid or v == dpid:
                    port_u = data["port_a"] if data["dpid_a"] == u else data["port_b"]
                    port_v = data["port_b"] if data["dpid_a"] == u else data["port_a"]
                    if (dpid == u and port_u == port_no) or (
                        dpid == v and port_v == port_no
                    ):
                        return True
            return False

    def copy_graph(self) -> nx.MultiGraph:
        """Return a thread-safe deep copy of the underlying NetworkX graph."""
        with self._lock:
            return self._graph.copy()

    @property
    def lock(self) -> threading.Lock:
        return self._lock


# ── TopologyManager (os-ken integration) ────────────────────────────────


class TopologyManager:
    """Watches os-ken topology events and keeps a ``TopologyGraph`` in sync.

    This is the bridge between os-ken's LLDP-based link discovery and our
    pure-Python graph model.  It converts os-ken event objects into
    graph mutations with zero business logic.
    """

    def __init__(self, graph: TopologyGraph) -> None:
        self.graph = graph

    def switch_enter(self, dp: Datapath) -> None:
        """Register a newly connected switch and all its ports."""
        dpid = dp.id
        port_count = 0
        self.graph.add_switch(dpid)
        for port in dp.ports.values():
            if port.port_no < 0xFFFFFFF0:
                self.graph.add_port(dpid, port.port_no)
                port_count += 1
        LOG.info(
            "TopoMgr: switch ENTER dpid=%s | %d ports registered", hex(dpid), port_count
        )

    def switch_leave(self, dp: Datapath) -> None:
        """Unregister a disconnected switch and all associated state."""
        LOG.info("TopoMgr: switch LEAVE dpid=%s", hex(dp.id))
        self.graph.remove_switch(dp.id)

    def port_add(self, dp: Datapath, port_no: int) -> None:
        """Register a newly added port (OFPPR_ADD)."""
        if port_no < 0xFFFFFFF0:
            LOG.info("TopoMgr: port ADD dpid=%s port=%d", hex(dp.id), port_no)
            self.graph.add_port(dp.id, port_no)

    def port_delete(self, dp: Datapath, port_no: int) -> None:
        """Remove a deleted port and any link that used it."""
        LOG.info("TopoMgr: port DELETE dpid=%s port=%d", hex(dp.id), port_no)
        self.graph.remove_port(dp.id, port_no)

    def port_modify(self, dp: Datapath, port_no: int, is_down: bool) -> None:
        """Handle a port state change (link up/down)."""
        status = "DOWN" if is_down else "UP"
        LOG.info(
            "TopoMgr: port MODIFY dpid=%s port=%d → %s", hex(dp.id), port_no, status
        )
        if is_down:
            self.graph.remove_port(dp.id, port_no)
        else:
            self.graph.add_port(dp.id, port_no)

    def link_add(self, link: Link) -> None:
        """Record a newly discovered switch-to-switch link (from LLDP)."""
        LOG.info(
            "TopoMgr: LINK ADD %s:%d → %s:%d",
            hex(link.src.dpid),
            link.src.port_no,
            hex(link.dst.dpid),
            link.dst.port_no,
        )
        lk = LinkKey(
            src_dpid=link.src.dpid,
            src_port=link.src.port_no,
            dst_dpid=link.dst.dpid,
            dst_port=link.dst.port_no,
        )
        self.graph.add_link(lk)

    def link_delete(self, link: Link) -> None:
        """Remove a timed-out switch-to-switch link (from LLDP)."""
        LOG.info(
            "TopoMgr: LINK DELETE %s:%d → %s:%d",
            hex(link.src.dpid),
            link.src.port_no,
            hex(link.dst.dpid),
            link.dst.port_no,
        )
        lk = LinkKey(
            src_dpid=link.src.dpid,
            src_port=link.src.port_no,
            dst_dpid=link.dst.dpid,
            dst_port=link.dst.port_no,
        )
        self.graph.remove_link(lk)

    def resolve_link(self, dpid: int, port_no: int) -> Optional[LinkKey]:
        """Find the ``LinkKey`` that uses *(dpid, port_no)* as either endpoint.

        Returns the directed link with *dpid* as the **source**, or None
        if the port is edge (host-facing, no link).
        """
        for lk in self.graph.links:
            if lk.src_dpid == dpid and lk.src_port == port_no:
                LOG.debug(
                    "TopoMgr: resolved (%s, %d) → link %s:%d → %s:%d",
                    hex(dpid),
                    port_no,
                    hex(lk.src_dpid),
                    lk.src_port,
                    hex(lk.dst_dpid),
                    lk.dst_port,
                )
                return lk
            if lk.dst_dpid == dpid and lk.dst_port == port_no:
                LOG.debug(
                    "TopoMgr: resolved (%s, %d) → reverse link %s:%d → %s:%d",
                    hex(dpid),
                    port_no,
                    hex(lk.dst_dpid),
                    lk.dst_port,
                    hex(lk.src_dpid),
                    lk.src_port,
                )
                return lk.reverse
        LOG.debug("TopoMgr: no link found for (%s, %d)", hex(dpid), port_no)
        return None
