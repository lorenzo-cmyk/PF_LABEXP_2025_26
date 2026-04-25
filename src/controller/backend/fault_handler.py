"""FaultHandler — coordinates response to link/port failures."""

from __future__ import annotations

import logging

from topology import LinkKey, TopologyGraph, TopologyManager
from spanning_tree import SpanningTreeManager
from forwarding_plane import ForwardingPlane
from flow_installer import FlowInstaller

LOG = logging.getLogger(__name__)


class FaultHandler:
    """Handles link failures: updates graph, recomputes ST, removes affected flows."""

    def __init__(
        self,
        graph: TopologyGraph,
        topo_mgr: TopologyManager,
        st_mgr: SpanningTreeManager,
        forwarding: ForwardingPlane,
        flow_installer: FlowInstaller,
    ) -> None:
        self.graph = graph
        self.topo_mgr = topo_mgr
        self.st_mgr = st_mgr
        self.forwarding = forwarding
        self.flow_installer = flow_installer

    def handle_port_down(self, dpid: int, port: int) -> None:
        """React to a port going down.

        Two distinct cases, driven by whether we can resolve a link:

        **Edge port** (link is None):
            The port was host-facing. Purge any hosts learned on it
            (they may reconnect elsewhere — mobility), delete their
            stale flows on all switches, and clean route tracker entries.

        **Switch-to-switch link** (link found):
            A link between switches failed. Let ForwardingPlane handle
            the affected flows. The port is removed from the graph
            afterward (which also tears down the link).
        """
        LOG.warning(
            "FaultHandler: PORT DOWN dpid=%s port=%d — starting recovery",
            hex(dpid),
            port,
        )

        link = self.topo_mgr.resolve_link(dpid, port)
        if link:
            LOG.info(
                "FaultHandler: resolved to link %s:%d → %s:%d",
                hex(link.src_dpid),
                link.src_port,
                hex(link.dst_dpid),
                link.dst_port,
            )
        else:
            LOG.info(
                "FaultHandler: port %s:%d is an edge port (no link to resolve)",
                hex(dpid),
                port,
            )

        # ── Edge port: purge hosts and their flows ───────────────────
        if link is None:
            removed = self.forwarding.host_tracker.remove_by_port(dpid, port)
            if removed:
                LOG.info(
                    "FaultHandler: purged hosts on disconnected edge port %s:%d: %s",
                    hex(dpid),
                    port,
                    ", ".join(removed),
                )
                # Delete stale flows for each purged host on *every* switch.
                # Without this, old flow entries would blackhole traffic
                # toward the now-dead port.
                for mac in removed:
                    for sw_dpid in self.graph.switches:
                        self.flow_installer.delete_flows_for_mac(sw_dpid, mac)
                    self.forwarding.route_tracker.purge_mac(mac)

        # ── Remove port from graph (also tears down any associated link)
        self.graph.remove_port(dpid, port)

        if link is not None:
            self.forwarding.handle_link_failure(link)

        # Recompute spanning tree and update flood rules
        self._refresh_flood_topology()
        LOG.info("FaultHandler: recovery complete for dpid=%s port=%d", hex(dpid), port)

    def handle_link_down(self, link: LinkKey) -> None:
        """React to a link going down (from LLDP/topology event)."""
        LOG.warning(
            "FaultHandler: LINK DOWN %s:%d → %s:%d — starting recovery",
            hex(link.src_dpid),
            link.src_port,
            hex(link.dst_dpid),
            link.dst_port,
        )

        self.graph.remove_link(link)
        self.forwarding.handle_link_failure(link)
        self._refresh_flood_topology()

        LOG.info(
            "FaultHandler: recovery complete for link %s:%d → %s:%d",
            hex(link.src_dpid),
            link.src_port,
            hex(link.dst_dpid),
            link.dst_port,
        )

    def _refresh_flood_topology(self) -> None:
        """Recompute ST and replace flood rules on all switches (preserves unicast flows)."""
        LOG.info("FaultHandler: refreshing flood topology")
        self.st_mgr.compute()
        for dpid in self.graph.switches:
            flood_ports = self.st_mgr.flood_ports(dpid)
            self.flow_installer.delete_flood_rule(dpid)
            if flood_ports:
                self.flow_installer.install_flood_rules(dpid, flood_ports)
        LOG.info(
            "FaultHandler: flood topology refreshed for %d switches",
            len(self.graph.switches),
        )
