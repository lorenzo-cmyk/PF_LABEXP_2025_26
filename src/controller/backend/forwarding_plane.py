"""ForwardingPlane — orchestrates path computation, route tracking, and flow installation."""

from __future__ import annotations

import logging
from typing import Optional

from host_tracker import HostTracker
from path_computer import PathComputer
from route_tracker import RouteTracker
from flow_installer import FlowInstaller
from policy_manager import PolicyManager
from topology import LinkKey

LOG = logging.getLogger(__name__)


class ForwardingPlane:
    """Decides which path to use and coordinates installation.

    Checks ``PolicyManager`` first: if a user-pinned path exists for the
    pair, it is installed with high priority and no idle timeout. Otherwise
    the default shortest-path is used.
    """

    def __init__(
        self,
        path_computer: PathComputer,
        route_tracker: RouteTracker,
        flow_installer: FlowInstaller,
        host_tracker: HostTracker,
        policy_mgr: PolicyManager,
    ) -> None:
        self.path_computer = path_computer
        self.route_tracker = route_tracker
        self.flow_installer = flow_installer
        self.host_tracker = host_tracker
        self.policy_mgr = policy_mgr
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

        # Learn source location — returns previous location if host moved
        old_loc = self.host_tracker.learn(src_mac, in_dpid, in_port)
        if old_loc is not None:
            # Host moved: purge stale flows on the old switch and clean
            # route tracker entries that involved this host from the old
            # location.  New flows will be installed below.
            LOG.info(
                "Forwarding: %s moved from dpid=%s → dpid=%s — cleaning old flows",
                src_mac,
                hex(old_loc.dpid),
                hex(in_dpid),
            )
            self.flow_installer.delete_flows_for_mac(old_loc.dpid, src_mac)
            self.route_tracker.purge_mac(src_mac)
            self.path_computer.invalidate_pair(old_loc.dpid, in_dpid)

        # Lookup destination
        dst_loc = self.host_tracker.lookup(dst_mac)
        if dst_loc is None:
            LOG.info("Forwarding: dst %s UNKNOWN — will flood (ARP?)", dst_mac)
            return False

        src_loc = self.host_tracker.lookup(src_mac)
        src_dpid = src_loc.dpid if src_loc else in_dpid
        dst_dpid = dst_loc.dpid

        # Only install paths from the source host's switch.  If this packet-in
        # arrived at an intermediate switch (e.g., because it was flooded before
        # flow rules existed), computing a path from here would corrupt the
        # route tracker by overwriting the correct source→destination route.
        if in_dpid != src_dpid:
            LOG.debug(
                "Forwarding: packet-in at dpid=%s but source %s lives on dpid=%s "
                "— skipping path install (existing flows or flood will deliver)",
                hex(in_dpid),
                src_mac,
                hex(src_dpid),
            )
            return True

        if src_dpid == dst_dpid:
            LOG.info(
                "Forwarding: same switch dpid=%s — installing direct flow",
                hex(src_dpid),
            )
            self.flow_installer.install_path([src_dpid], src_mac, dst_mac)
            self.flow_installer.install_path([src_dpid], dst_mac, src_mac)
            return True

        # Check for a user-pinned policy path first
        policy_path = self.policy_mgr.get_policy_path(src_mac, dst_mac)
        if policy_path is not None:
            dpids = [policy_path[0].src_dpid]
            for lk in policy_path:
                dpids.append(lk.dst_dpid)
            LOG.info(
                "Forwarding: using POLICY path %s → %s: %s",
                src_mac,
                dst_mac,
                " → ".join(hex(d) for d in dpids),
            )
            # Install forward + reverse (sink-to-source, high priority, no timeout)
            links = self.flow_installer.install_path(
                dpids, src_mac, dst_mac, is_policy=True
            )
            reverse_dpids = list(reversed(dpids))
            reverse_links = self.flow_installer.install_path(
                reverse_dpids, dst_mac, src_mac, is_policy=True
            )
            if links:
                self.route_tracker.add_route(src_mac, dst_mac, links)
            if reverse_links:
                self.route_tracker.add_route(dst_mac, src_mac, reverse_links)
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
        """Called by FaultHandler. Removes affected flows and returns affected pairs.

        The recovery strategy:
        1. Query RouteTracker for every (src_mac, dst_mac) pair whose path
           traverses the failed link (using the undirected key, which matches
           regardless of link direction).
        2. For each affected pair, delete both src_mac and dst_mac flows on
           every switch that was on the old path.
        3. Remove the pair from RouteTracker so a new path is computed on the
           next packet-in.
        4. Invalidate the entire path cache since any cached path may now be
           invalid after the topology change.
        """
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
            # Delete flows matching both MACs on every switch that carried
            # this pair's traffic.  ``delete_flows_for_mac`` is idempotent
            # — if the flows already timed out, the delete is a safe no-op.
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
