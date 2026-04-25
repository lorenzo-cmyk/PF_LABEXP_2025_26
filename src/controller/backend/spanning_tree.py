"""SpanningTreeManager — computes a BFS spanning tree for loop-free flooding."""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional


from topology import TopologyGraph

LOG = logging.getLogger(__name__)


class SpanningTreeManager:
    """Maintains a logical spanning tree over ``TopologyGraph`` for broadcast flooding.

    Does NOT disable ports physically. Returns the set of ports that
    should be used for flooding broadcast traffic.

    The tree is built via BFS from the lowest dpid node.  Disconnected
    components (after a network partition) each get their own root and
    are independently spanned by the BFS outer loop.
    """

    def __init__(self, graph: TopologyGraph) -> None:
        self.graph = graph
        self._tree_edges: set[tuple[int, int]] = set()
        self._root: Optional[int] = None

    def compute(self) -> None:
        """Recompute the spanning tree from the current graph snapshot.

        Uses BFS starting from the lowest dpid.  The outer loop over
        ``sorted(g.nodes)`` ensures deterministic behaviour across
        disconnected components (each component gets its first BFS from
        the smallest node in that component).
        """
        g = self.graph.copy_graph()
        if not g.nodes:
            self._tree_edges = set()
            self._root = None
            LOG.info("ST: empty graph — no spanning tree")
            return

        root = min(g.nodes)
        self._root = root

        visited = set()
        tree_edges: set[tuple[int, int]] = set()

        for node in sorted(g.nodes):
            if node not in visited:
                visited.add(node)
                queue: deque[int] = deque([node])
                while queue:
                    curr = queue.popleft()
                    for neighbor in g.neighbors(curr):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            tree_edges.add((curr, neighbor))
                            tree_edges.add((neighbor, curr))
                            queue.append(neighbor)

        self._tree_edges = tree_edges

        # Log summary
        undirected = {(min(u, v), max(u, v)) for u, v in tree_edges}
        LOG.info(
            "ST: computed | root=%s | tree_edges=%d | switches=%d",
            hex(root),
            len(undirected),
            len(g.nodes),
        )
        for u, v in sorted(undirected):
            LOG.debug("ST: tree edge %s — %s", hex(u), hex(v))

    def flood_ports(self, dpid: int) -> set[int]:
        """Return ports on *dpid* that should participate in broadcast flooding.

        The returned set is the union of:
        1. All edge (host-facing) ports on the switch, and
        2. Internal ports that belong to the active spanning tree.
        """
        ports: set[int] = set()

        # Edge ports (host-facing) always included so connected hosts
        # receive ARP and other broadcast traffic.
        for sw, port in self.graph.edge_ports:
            if sw == dpid:
                ports.add(port)

        # Internal ports that are part of the spanning tree.
        # Non-tree internal ports are excluded to prevent loops.
        g = self.graph.copy_graph()
        if dpid not in g:
            LOG.debug("ST: flood_ports dpid=%s not in graph", hex(dpid))
            return ports
        for neighbor in g.neighbors(dpid):
            if (dpid, neighbor) in self._tree_edges:
                port = self.graph.get_port_for_peer(dpid, neighbor)
                if port is not None:
                    ports.add(port)

        LOG.debug("ST: flood_ports dpid=%s → ports=%s", hex(dpid), sorted(ports))
        return ports

    @property
    def tree_edges(self) -> set[tuple[int, int]]:
        """Current spanning tree edges as directed (dpid_a, dpid_b) pairs."""
        return set(self._tree_edges)

    @property
    def root(self) -> Optional[int]:
        """The dpid of the BFS root, or None if the graph is empty."""
        return self._root
