"""ForwardingPlane — orchestrates path computation, route tracking, and flow installation."""

from __future__ import annotations

import logging
from typing import Optional

from host_tracker import HostTracker
from path_computer import PathComputer
from route_tracker import RouteTracker
from flow_installer import FlowInstaller
from topology import LinkKey

LOG = logging.getLogger(__name__)


class ForwardingPlane:
    """Decides which path to use and coordinates installation.

    For GOAL 1: always uses default shortest-path (no policy plane yet).
    """

    def __init__(
        self,
        path_computer: PathComputer,
        route_tracker: RouteTracker,
        flow_installer: FlowInstaller,
        host_tracker: HostTracker,
    ) -> None:
        self.path_computer = path_computer
        self.route_tracker = route_tracker
        self.flow_installer = flow_installer
        self.host_tracker = host_tracker
        # Wire host tracker reference into flow installer for edge port lookup
        self.flow_installer._host_tracker = host_tracker

    def handle_packet(
        self, src_mac: str, dst_mac: str, in_dpid: int, in_port: int
    ) -> bool:
        """Handle a packet-in for unicast forwarding.

        Returns True if a path was installed, False if unreachable or unknown dst.
        """
        LOG.info(
            "Forwarding: packet-in %s → %s on dpid=%s port=%d",
            src_mac,
            dst_mac,
            hex(in_dpid),
            in_port,
        )

        # Learn source location
        self.host_tracker.learn(src_mac, in_dpid, in_port)

        # Lookup destination
        dst_loc = self.host_tracker.lookup(dst_mac)
        if dst_loc is None:
            LOG.info("Forwarding: dst %s UNKNOWN — will flood (ARP?)", dst_mac)
            return False

        src_dpid = in_dpid
        dst_dpid = dst_loc.dpid

        if src_dpid == dst_dpid:
            LOG.info(
                "Forwarding: same switch dpid=%s — installing direct flow",
                hex(src_dpid),
            )
            self.flow_installer.install_path([src_dpid], src_mac, dst_mac)
            return True

        # Compute shortest path
        path = self.path_computer.compute_path(src_dpid, dst_dpid)
        if path is None:
            LOG.warning(
                "Forwarding: NO PATH %s(dpid=%s) → %s(dpid=%s) — network partitioned?",
                src_mac,
                hex(src_dpid),
                dst_mac,
                hex(dst_dpid),
            )
            return False

        # Install forward path (sink-to-source)
        links = self.flow_installer.install_path(path, src_mac, dst_mac)

        # Install reverse path for symmetry
        reverse_path = list(reversed(path))
        reverse_links = self.flow_installer.install_path(reverse_path, dst_mac, src_mac)

        # Track routes for fault recovery
        if links:
            self.route_tracker.add_route(src_mac, dst_mac, links)
        if reverse_links:
            self.route_tracker.add_route(dst_mac, src_mac, reverse_links)

        LOG.info(
            "Forwarding: path installed %s → %s | fwd_links=%d rev_links=%d",
            src_mac,
            dst_mac,
            len(links),
            len(reverse_links),
        )
        return True

    def get_output_port(self, src_dpid: int, dst_dpid: int) -> Optional[int]:
        """Return the output port on src_dpid toward dst_dpid (first hop of path)."""
        path = self.path_computer.compute_path(src_dpid, dst_dpid)
        if path is None or len(path) < 2:
            return None
        port = self.flow_installer.graph.get_port_for_peer(path[0], path[1])
        LOG.debug(
            "Forwarding: output_port %s → %s = port %d",
            hex(src_dpid),
            hex(dst_dpid),
            port if port else -1,
        )
        return port

    def handle_link_failure(self, link: LinkKey) -> list[tuple[str, str]]:
        """Called by FaultHandler. Removes affected flows and returns affected pairs."""
        LOG.warning(
            "Forwarding: link failure %s:%d → %s:%d — finding affected flows",
            hex(link.src_dpid),
            link.src_port,
            hex(link.dst_dpid),
            link.dst_port,
        )

        affected = self.route_tracker.pairs_on_link(link)
        if not affected:
            LOG.info("Forwarding: no tracked flows on failed link — nothing to clean")
            self.path_computer.invalidate()
            return []

        for src_mac, dst_mac in affected:
            pair_links = self.route_tracker.links_for_pair(src_mac, dst_mac)
            dpids_to_clean: set[int] = set()
            for lk in pair_links:
                dpids_to_clean.add(lk.src_dpid)
                dpids_to_clean.add(lk.dst_dpid)
            for dpid in dpids_to_clean:
                self.flow_installer.delete_flows_for_mac(dpid, dst_mac)
                self.flow_installer.delete_flows_for_mac(dpid, src_mac)
            self.route_tracker.remove_route(src_mac, dst_mac)
            self.route_tracker.remove_route(dst_mac, src_mac)
            LOG.info(
                "Forwarding: cleaned %d switches for pair %s ↔ %s",
                len(dpids_to_clean),
                src_mac,
                dst_mac,
            )

        self.path_computer.invalidate()
        LOG.info("Forwarding: link failure handled | %d affected pairs", len(affected))
        return list(affected)
