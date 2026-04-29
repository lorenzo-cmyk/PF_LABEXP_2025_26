"""FaultHandler — coordinates response to link/port failures."""

from __future__ import annotations

import logging

from topology import LinkKey, TopologyGraph, TopologyManager
from forwarding_plane import ForwardingPlane
from flow_installer import FlowInstaller
from policy_manager import PolicyManager

LOG = logging.getLogger(__name__)


class FaultHandler:
    """Handles link failures: updates graph, removes affected flows.

    Without spanning-tree flooding, only the graph and affected flows
    need to be cleaned up on topology changes.
    """

    def __init__(
        self,
        graph: TopologyGraph,
        topo_mgr: TopologyManager,
        forwarding: ForwardingPlane,
        flow_installer: FlowInstaller,
        policy_mgr: PolicyManager,
    ) -> None:
        self.graph = graph
        self.topo_mgr = topo_mgr
        self.forwarding = forwarding
        self.flow_installer = flow_installer
        self.policy_mgr = policy_mgr

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
        is_internal = self.graph.is_known_internal(dpid, port)

        if link:
            LOG.info(
                "FaultHandler: resolved to link %s:%d → %s:%d",
                hex(link.src_dpid),
                link.src_port,
                hex(link.dst_dpid),
                link.dst_port,
            )
        elif is_internal:
            LOG.info(
                "FaultHandler: port %s:%d was a link port (link already torn by peer "
                "— skipping edge-port cleanup)",
                hex(dpid),
                port,
            )
        else:
            LOG.info(
                "FaultHandler: port %s:%d is an edge port (no link to resolve)",
                hex(dpid),
                port,
            )

        # ── Edge port: purge hosts and their flows ───────────────────
        if link is None and not is_internal:
            removed = self.forwarding.host_tracker.remove_by_port(dpid, port)
            if removed:
                LOG.info(
                    "FaultHandler: purged hosts on disconnected edge port %s:%d: %s",
                    hex(dpid),
                    port,
                    ", ".join(removed),
                )
                for mac in removed:
                    for sw_dpid in self.graph.switches:
                        self.flow_installer.delete_flows_for_mac(sw_dpid, mac)
                    purged = self.forwarding.route_tracker.purge_mac(mac)
                    # Also delete reverse-direction flows for each purged pair
                    for pair in purged:
                        for sw_dpid in self.graph.switches:
                            self.flow_installer.delete_flows_for_mac(sw_dpid, pair[0])
                            self.flow_installer.delete_flows_for_mac(sw_dpid, pair[1])
                    # Mark all policies involving this MAC as BROKEN
                    self.policy_mgr.mark_all_for_mac_broken(mac)

        # ── Remove port from graph (also tears down any associated link)
        self.graph.remove_port(dpid, port)

        if link is not None:
            self.forwarding.handle_link_failure(link)
            self.policy_mgr.mark_all_affected_broken(link)

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
        self.policy_mgr.mark_all_affected_broken(link)

        LOG.info(
            "FaultHandler: recovery complete for link %s:%d → %s:%d",
            hex(link.src_dpid),
            link.src_port,
            hex(link.dst_dpid),
            link.dst_port,
        )
