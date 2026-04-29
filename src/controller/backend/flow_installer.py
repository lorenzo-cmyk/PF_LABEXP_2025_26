"""FlowInstaller — single write point for OpenFlow flow-mod messages.

Installs paths sink-to-source to minimize the inconsistency window.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from os_ken.controller.controller import Datapath


from topology import LinkKey, TopologyGraph

LOG = logging.getLogger(__name__)

# Default idle timeout for regular (non-policy) flows in seconds.
DEFAULT_IDLE_TIMEOUT = 30
# Table 0 priority levels
PRIORITY_DROP_IPV6 = 50
PRIORITY_DROP_IPV4_MCAST = 40
PRIORITY_POLICY = 20
PRIORITY_DEFAULT = 10


class FlowInstaller:
    """Installs and removes flows on switches. Only module that touches OpenFlow."""

    def __init__(self, graph: TopologyGraph) -> None:
        self.graph = graph
        self._datapaths: dict[int, Datapath] = {}
        self._host_tracker: Optional[object] = None  # set by ForwardingPlane

    def register_dp(self, dp: Datapath) -> None:
        """Store a datapath handle for later flow-mod operations.

        Called from ``Backend._switch_features_handler()`` when a switch
        completes the OpenFlow handshake.  The datapath is needed to
        send ``OFPFlowMod`` and ``OFPPacketOut`` messages to the switch.
        """
        self._datapaths[dp.id] = dp
        LOG.info(
            "FlowInstaller: registered datapath dpid=%s | total=%d",
            hex(dp.id),
            len(self._datapaths),
        )

    def unregister_dp(self, dpid: int) -> None:
        """Forget a datapath (e.g., switch disconnected).

        Once unregistered, no further ``OFPFlowMod`` or ``OFPPacketOut``
        messages will be sent to that switch.  Existing flows on the
        switch are *not* removed — the switch will eventually time them
        out or the surviving switches' ``delete_flows_for_mac()`` calls
        during ``purge_switch()`` will not target this dpid.
        """
        self._datapaths.pop(dpid, None)
        LOG.info(
            "FlowInstaller: unregistered datapath dpid=%s | total=%d",
            hex(dpid),
            len(self._datapaths),
        )

    @property
    def datapaths(self) -> dict[int, Datapath]:
        """Return a snapshot of all connected datapaths (for external polling).

        Used by ``StatsCollector`` to iterate all switches when sending
        ``OFPPortStatsRequest``.  The internal dict is shallow-copied so
        callers can safely iterate without holding a lock.
        """
        return dict(self._datapaths)

    def get_dp(self, dpid: int) -> Optional[Datapath]:
        """Return the Datapath handle for *dpid*, or None if not connected.

        Used by ``Backend`` during stale-disconnect detection: compares
        the current registered datapath with the one firing the DEAD
        event to decide whether to ignore the disconnect.
        """
        return self._datapaths.get(dpid)

    # ── Baseline drop rules ──────────────────────────────────────────────

    def install_drop_rules(self, dp: Datapath) -> None:
        """Install high-priority drop rules for IPv6 and IPv4 Multicast.

        These are permanent (idle_timeout=0) and must be installed once per
        switch connection.
        """
        ofp_parser = dp.ofproto_parser

        # Drop all IPv6 (EtherType 0x86DD)
        match_ipv6 = ofp_parser.OFPMatch(eth_type=0x86DD)
        insts = []  # empty instructions = drop
        self._send_flow_mod(
            dp,
            match=match_ipv6,
            instructions=insts,
            priority=PRIORITY_DROP_IPV6,
            idle_timeout=0,
            hard_timeout=0,
        )
        LOG.info("FlowInstaller: installed IPv6 DROP on dpid=%s", hex(dp.id))

        # Drop IPv4 Multicast (EtherType 0x0800 + Dst MAC 01:00:5E:xx:xx:xx/24)
        # The match: eth_type=0x0800 AND eth_dst matches 01:00:5e:*:*:*
        match_mcast = ofp_parser.OFPMatch(
            eth_type=0x0800,
            eth_dst=("01:00:5e:00:00:00", "ff:ff:ff:80:00:00"),
        )
        insts_mcast = []
        self._send_flow_mod(
            dp,
            match=match_mcast,
            instructions=insts_mcast,
            priority=PRIORITY_DROP_IPV4_MCAST,
            idle_timeout=0,
            hard_timeout=0,
        )
        LOG.info("FlowInstaller: installed IPv4 Multicast DROP on dpid=%s", hex(dp.id))

    # ── Install a unicast path (sink → source) ──────────────────────────

    def install_path(
        self, path: list[int], src_mac: str, dst_mac: str, *, is_policy: bool = False
    ) -> list[LinkKey]:
        """Install flow entries along *path* (list of dpids) for dst_mac.

        Installs sink-to-source (last switch first). Returns the list of
        LinkKey objects traversed, for RouteTracker.
        """
        timeout = 0 if is_policy else DEFAULT_IDLE_TIMEOUT
        priority = PRIORITY_POLICY if is_policy else PRIORITY_DEFAULT
        path_str = " → ".join(hex(d) for d in path)
        LOG.info(
            "FlowInstaller: install_path %s → %s via [%s] (timeout=%d, policy=%s)",
            src_mac,
            dst_mac,
            path_str,
            timeout,
            is_policy,
        )

        if len(path) < 2:
            # Single-switch path: install direct edge flow
            if len(path) == 1:
                dp = self._datapaths.get(path[0])
                if dp:
                    out_port = self._find_edge_port(path[0], dst_mac)
                    if out_port is not None:
                        self._add_flow(dp, dst_mac, out_port, timeout, priority)
                        LOG.info(
                            "FlowInstaller: single-switch flow dpid=%s dst=%s port=%d",
                            hex(path[0]),
                            dst_mac,
                            out_port,
                        )
            return []

        links: list[LinkKey] = []

        # ── Walk the path backwards: sink → … → source ─────────────────
        for i in range(len(path) - 1, 0, -1):
            dpid = path[i]
            dp = self._datapaths.get(dpid)
            if dp is None:
                LOG.warning(
                    "FlowInstaller: datapath dpid=%s not connected — skipping",
                    hex(dpid),
                )
                continue

            if i == len(path) - 1:
                # ── Sink switch: output to edge port (host-facing) ──
                out_port = self._find_edge_port(dpid, dst_mac)
                if out_port is not None:
                    self._add_flow(dp, dst_mac, out_port, timeout, priority)
                    LOG.info(
                        "FlowInstaller:   [sink] dpid=%s eth_dst=%s port=%d (edge)",
                        hex(dpid),
                        dst_mac,
                        out_port,
                    )
                else:
                    LOG.warning(
                        "FlowInstaller:   [sink] dpid=%s no edge port for %s",
                        hex(dpid),
                        dst_mac,
                    )
            else:
                # ── Intermediate switch: output toward next hop ──
                next_dpid = path[i + 1]
                out_port = self.graph.get_port_for_peer(dpid, next_dpid)
                if out_port is not None:
                    self._add_flow(dp, dst_mac, out_port, timeout, priority)
                    LOG.info(
                        "FlowInstaller:   [mid]  dpid=%s eth_dst=%s port=%d (→ %s)",
                        hex(dpid),
                        dst_mac,
                        out_port,
                        hex(next_dpid),
                    )

            # ── Build LinkKey for RouteTracker ──
            if i >= len(path) - 1:
                continue
            next_dpid = path[i + 1]
            src_port = self.graph.get_port_for_peer(dpid, next_dpid)
            dst_port = self.graph.get_port_for_peer(next_dpid, dpid)
            if src_port is not None and dst_port is not None:
                links.append(
                    LinkKey(
                        src_dpid=dpid,
                        src_port=src_port,
                        dst_dpid=next_dpid,
                        dst_port=dst_port,
                    )
                )

        # ── Source switch (path[0]): output toward second hop ────────
        src_dpid = path[0]
        next_dpid = path[1]
        dp = self._datapaths.get(src_dpid)
        out_port = self.graph.get_port_for_peer(src_dpid, next_dpid)
        if dp and out_port is not None:
            self._add_flow(dp, dst_mac, out_port, timeout, priority)
            LOG.info(
                "FlowInstaller:   [src]  dpid=%s eth_dst=%s port=%d (→ %s)",
                hex(src_dpid),
                dst_mac,
                out_port,
                hex(next_dpid),
            )

        dst_port = self.graph.get_port_for_peer(next_dpid, src_dpid)
        if out_port is not None and dst_port is not None:
            links.append(
                LinkKey(
                    src_dpid=src_dpid,
                    src_port=out_port,
                    dst_dpid=next_dpid,
                    dst_port=dst_port,
                )
            )

        LOG.info(
            "FlowInstaller: install_path done | %d links tracked for %s → %s",
            len(links),
            src_mac,
            dst_mac,
        )
        return links

    # ── Flow deletion ───────────────────────────────────────────────────

    def delete_flows_for_mac(self, dpid: int, dst_mac: str) -> None:
        """Remove all flows matching dst_mac on a switch."""
        dp = self._datapaths.get(dpid)
        if dp is None:
            return
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        match = ofp_parser.OFPMatch(eth_dst=dst_mac)
        msg = ofp_parser.OFPFlowMod(
            datapath=dp,
            match=match,
            command=ofp.OFPFC_DELETE,
            table_id=ofp.OFPTT_ALL,
            out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY,
        )
        dp.send_msg(msg)
        LOG.info("FlowInstaller: delete_flows dpid=%s eth_dst=%s", hex(dpid), dst_mac)

    def delete_flows_on_port(self, dpid: int, port: int) -> None:
        """Remove all flows that output to a specific port."""
        dp = self._datapaths.get(dpid)
        if dp is None:
            return
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        msg = ofp_parser.OFPFlowMod(
            datapath=dp,
            command=ofp.OFPFC_DELETE,
            table_id=ofp.OFPTT_ALL,
            out_port=port,
            out_group=ofp.OFPG_ANY,
        )
        dp.send_msg(msg)
        LOG.info("FlowInstaller: delete_flows_on_port dpid=%s port=%d", hex(dpid), port)

    # ── Packet-out helpers ──────────────────────────────────────────────

    def send_packet_out(
        self, dp: Datapath, data: bytes, buffer_id: int, in_port: int, out_port: int
    ) -> None:
        """Send a packet out a specific port."""
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        actions = [ofp_parser.OFPActionOutput(out_port)]
        out = ofp_parser.OFPPacketOut(
            datapath=dp,
            buffer_id=buffer_id,
            in_port=in_port,
            actions=actions,
            data=data if buffer_id == ofp.OFP_NO_BUFFER else None,
        )
        dp.send_msg(out)
        LOG.debug(
            "FlowInstaller: packet-out dpid=%s in=%d → out=%d",
            hex(dp.id),
            in_port,
            out_port,
        )

    # ── Internal helpers ────────────────────────────────────────────────

    def _add_flow(
        self,
        dp: Datapath,
        dst_mac: str,
        out_port: int,
        idle_timeout: int,
        priority: int,
    ) -> None:
        """Install a unicast flow matching *dst_mac* → output *out_port*."""
        ofp_parser = dp.ofproto_parser
        match = ofp_parser.OFPMatch(eth_dst=dst_mac)
        actions = [ofp_parser.OFPActionOutput(out_port)]
        self._send_flow_mod(
            dp,
            match=match,
            actions=actions,
            priority=priority,
            idle_timeout=idle_timeout,
            hard_timeout=0,
        )

    def _send_flow_mod(
        self,
        dp: Datapath,
        *,
        match,
        instructions=None,
        actions=None,
        priority: int,
        idle_timeout: int,
        hard_timeout: int,
        cookie: int = 0,
    ) -> None:
        """Build and send a single OFPFlowMod message to *dp*.

        If *instructions* is None and *actions* is provided, builds
        OFPInstructionActions.  If both are None, the flow drops.
        """
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        if instructions is None:
            if actions:
                instructions = [
                    ofp_parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)
                ]
            else:
                instructions = []
        msg = ofp_parser.OFPFlowMod(
            datapath=dp,
            cookie=cookie,
            match=match,
            instructions=instructions,
            priority=priority,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            buffer_id=ofp.OFP_NO_BUFFER,
        )
        dp.send_msg(msg)

    def _find_edge_port(self, dpid: int, dst_mac: str) -> Optional[int]:
        """Find the edge port on *dpid* for the destination host."""
        ht = self._host_tracker
        if ht is not None:
            loc = ht.lookup(dst_mac)
            if loc and loc.dpid == dpid:
                return loc.port
        LOG.warning(
            "FlowInstaller: no edge port found for %s on dpid=%s", dst_mac, hex(dpid)
        )
        return None
