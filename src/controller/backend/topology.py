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
    Edges: (dpid_a, dpid_b) with attributes storing both port numbers.
    Edge ports (host-facing) are tracked as a set of ``(dpid, port_no)`` tuples.
    """

    def __init__(self) -> None:
        self._graph = nx.Graph()
        self._lock = threading.Lock()
        self._edge_ports: set[tuple[int, int]] = set()
        self._switch_ports: dict[int, set[int]] = {}
        self._known_internal_ports: set[tuple[int, int]] = set()

    # -- graph mutations --------------------------------------------------

    def add_switch(self, dpid: int) -> None:
        with self._lock:
            self._graph.add_node(dpid)
            self._switch_ports.setdefault(dpid, set())
        LOG.info(
            "Graph: added switch dpid=%s | switches=%d",
            hex(dpid),
            len(self._graph.nodes),
        )

    def remove_switch(self, dpid: int) -> None:
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
        removed_links: list[str] = []
        with self._lock:
            self._switch_ports.get(dpid, set()).discard(port_no)
            self._edge_ports.discard((dpid, port_no))
            edges_to_remove = []
            for u, v, data in self._graph.edges(data=True):
                port_u = data["port_a"] if data["dpid_a"] == u else data["port_b"]
                port_v = data["port_b"] if data["dpid_a"] == u else data["port_a"]
                if (u == dpid and port_u == port_no) or (
                    v == dpid and port_v == port_no
                ):
                    edges_to_remove.append((u, v))
            for u, v in edges_to_remove:
                self._graph.remove_edge(u, v)
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
        with self._lock:
            a, b = min(link.src_dpid, link.dst_dpid), max(link.src_dpid, link.dst_dpid)
            port_a = link.src_port if link.src_dpid == a else link.dst_port
            port_b = link.dst_port if link.dst_dpid == b else link.src_port
            self._graph.add_edge(a, b, dpid_a=a, port_a=port_a, dpid_b=b, port_b=port_b)
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
        with self._lock:
            if self._graph.has_edge(link.src_dpid, link.dst_dpid):
                self._graph.remove_edge(link.src_dpid, link.dst_dpid)
                removed = True
            else:
                removed = False
            # Never automatically turn broken switch links back into edge ports.
            # If it's a switch-to-switch link, it stays known as not an edge port
            # even if the link is currently timed out by LLDP. This prevents
            # broadcast storms on active switch ports missing LLDP.
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
        with self._lock:
            return list(self._graph.nodes())

    @property
    def edge_ports(self) -> set[tuple[int, int]]:
        with self._lock:
            return set(self._edge_ports)

    @property
    def links(self) -> list[LinkKey]:
        with self._lock:
            result = []
            for u, v, data in self._graph.edges(data=True):
                a, b = data["dpid_a"], data["dpid_b"]
                port_a, port_b = data["port_a"], data["port_b"]
                result.append(LinkKey(a, port_a, b, port_b))
            return result

    def get_port_for_peer(self, src_dpid: int, dst_dpid: int) -> Optional[int]:
        with self._lock:
            if not self._graph.has_edge(src_dpid, dst_dpid):
                return None
            data = self._graph.edges[src_dpid, dst_dpid]
            if data["dpid_a"] == src_dpid:
                return data["port_a"]
            return data["port_b"]

    def copy_graph(self) -> nx.Graph:
        with self._lock:
            return self._graph.copy()

    @property
    def lock(self) -> threading.Lock:
        return self._lock


# ── TopologyManager (os-ken integration) ────────────────────────────────


class TopologyManager:
    """Watches os-ken topology events and keeps a ``TopologyGraph`` in sync."""

    def __init__(self, graph: TopologyGraph) -> None:
        self.graph = graph

    def switch_enter(self, dp: Datapath) -> None:
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
        LOG.info("TopoMgr: switch LEAVE dpid=%s", hex(dp.id))
        self.graph.remove_switch(dp.id)

    def port_add(self, dp: Datapath, port_no: int) -> None:
        if port_no < 0xFFFFFFF0:
            LOG.info("TopoMgr: port ADD dpid=%s port=%d", hex(dp.id), port_no)
            self.graph.add_port(dp.id, port_no)

    def port_delete(self, dp: Datapath, port_no: int) -> None:
        LOG.info("TopoMgr: port DELETE dpid=%s port=%d", hex(dp.id), port_no)
        self.graph.remove_port(dp.id, port_no)

    def port_modify(self, dp: Datapath, port_no: int, is_down: bool) -> None:
        status = "DOWN" if is_down else "UP"
        LOG.info(
            "TopoMgr: port MODIFY dpid=%s port=%d → %s", hex(dp.id), port_no, status
        )
        if is_down:
            self.graph.remove_port(dp.id, port_no)
        else:
            self.graph.add_port(dp.id, port_no)

    def link_add(self, link: Link) -> None:
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
