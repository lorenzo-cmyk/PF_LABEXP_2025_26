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
PRIORITY_DEFAULT = 10
PRIORITY_FLOOD = 1
# Cookie mask for identifying flood rules
FLOOD_COOKIE_BASE = 0xF100D00000000000
FLOOD_COOKIE_MASK = 0xFFFF000000000000


class FlowInstaller:
    """Installs and removes flows on switches. Only module that touches OpenFlow."""

    def __init__(self, graph: TopologyGraph) -> None:
        self.graph = graph
        self._datapaths: dict[int, Datapath] = {}
        self._host_tracker: Optional[object] = None  # set by ForwardingPlane

    def register_dp(self, dp: Datapath) -> None:
        self._datapaths[dp.id] = dp
        LOG.info(
            "FlowInstaller: registered datapath dpid=%s | total=%d",
            hex(dp.id),
            len(self._datapaths),
        )

    def unregister_dp(self, dpid: int) -> None:
        self._datapaths.pop(dpid, None)
        LOG.info(
            "FlowInstaller: unregistered datapath dpid=%s | total=%d",
            hex(dpid),
            len(self._datapaths),
        )

    def get_dp(self, dpid: int) -> Optional[Datapath]:
        return self._datapaths.get(dpid)

    # ── Install a unicast path (sink → source) ──────────────────────────

    def install_path(
        self, path: list[int], src_mac: str, dst_mac: str, *, is_policy: bool = False
    ) -> list[LinkKey]:
        """Install flow entries along *path* (list of dpids) for dst_mac.

        Installs sink-to-source (last switch first). Returns the list of
        LinkKey objects traversed, for RouteTracker.
        """
        timeout = 0 if is_policy else DEFAULT_IDLE_TIMEOUT
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
                    out_port = self._find_edge_port(path[0], src_mac, dst_mac)
                    if out_port is not None:
                        self._add_flow(dp, dst_mac, out_port, timeout, PRIORITY_DEFAULT)
                        LOG.info(
                            "FlowInstaller: single-switch flow dpid=%s dst=%s port=%d",
                            hex(path[0]),
                            dst_mac,
                            out_port,
                        )
            return []

        links: list[LinkKey] = []

        # Walk the path backwards (sink → source)
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
                # Last (sink) switch: output to edge port (host-facing)
                out_port = self._find_edge_port(dpid, src_mac, dst_mac)
                if out_port is not None:
                    self._add_flow(dp, dst_mac, out_port, timeout, PRIORITY_DEFAULT)
                    LOG.info(
                        "FlowInstaller:   [sink] dpid=%s eth_dst=%s → port=%d (edge)",
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
                # Intermediate switch: output toward next hop
                next_dpid = path[i + 1]
                out_port = self.graph.get_port_for_peer(dpid, next_dpid)
                if out_port is not None:
                    self._add_flow(dp, dst_mac, out_port, timeout, PRIORITY_DEFAULT)
                    LOG.info(
                        "FlowInstaller:   [mid]  dpid=%s eth_dst=%s → port=%d (→ %s)",
                        hex(dpid),
                        dst_mac,
                        out_port,
                        hex(next_dpid),
                    )

            # Record link with real port numbers
            if i >= len(path) - 1:
                continue  # sink has no next hop to record
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

        # Source switch (path[0]): output toward second hop
        src_dpid = path[0]
        next_dpid = path[1]
        dp = self._datapaths.get(src_dpid)
        out_port = self.graph.get_port_for_peer(src_dpid, next_dpid)
        if dp and out_port is not None:
            self._add_flow(dp, dst_mac, out_port, timeout, PRIORITY_DEFAULT)
            LOG.info(
                "FlowInstaller:   [src]  dpid=%s eth_dst=%s → port=%d (→ %s)",
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

    # ── Flood rules ─────────────────────────────────────────────────────

    def install_flood_rules(self, dpid: int, flood_ports: set[int]) -> None:
        """Install a low-priority rule that floods broadcast traffic to *flood_ports*.

        Matches eth_dst=ff:ff:ff:ff:ff:ff so that only broadcast packets (ARP,
        DHCP, etc.) are flooded.  Unicast packets fall through to the table-miss
        rule and are sent to the controller for path computation.
        """
        dp = self._datapaths.get(dpid)
        if dp is None or not flood_ports:
            return

        # Delete any existing flood rule first (idempotent reinstall)
        self.delete_flood_rule(dpid)

        ofp_parser = dp.ofproto_parser

        # Single rule matching broadcast destination — no per-input-port rules.
        # This avoids the in_port-only match bug that would intercept unicast
        # traffic before the table-miss rule gets a chance to send it to the
        # controller.
        match = ofp_parser.OFPMatch(eth_dst="ff:ff:ff:ff:ff:ff")
        actions = [
            ofp_parser.OFPActionOutput(port) for port in sorted(flood_ports)
        ]

        self._send_flow_mod(
            dp,
            match=match,
            actions=actions,
            priority=PRIORITY_FLOOD,
            idle_timeout=0,
            hard_timeout=0,
            cookie=FLOOD_COOKIE_BASE | (dpid & 0xFFFF),
        )

        LOG.info(
            "FlowInstaller: flood rule dpid=%s → ports=%s",
            hex(dpid),
            sorted(flood_ports),
        )

    def delete_flood_rule(self, dpid: int) -> None:
        """Delete the flood rule on a switch (cookie-based, preserves unicast flows)."""
        dp = self._datapaths.get(dpid)
        if dp is None:
            return
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        match = ofp_parser.OFPMatch(eth_dst="ff:ff:ff:ff:ff:ff")
        msg = ofp_parser.OFPFlowMod(
            datapath=dp,
            cookie=FLOOD_COOKIE_BASE | (dpid & 0xFFFF),
            cookie_mask=FLOOD_COOKIE_MASK,
            command=ofp.OFPFC_DELETE,
            table_id=ofp.OFPTT_ALL,
            out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY,
            match=match,
        )
        dp.send_msg(msg)
        LOG.debug("FlowInstaller: deleted flood rule dpid=%s", hex(dpid))

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

    def flood_packet_out(
        self,
        dp: Datapath,
        in_port: int,
        flood_ports: set[int],
        data: bytes,
        buffer_id: int,
    ) -> None:
        """Send a packet out a set of ports (broadcast flooding)."""
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        ports = flood_ports - {in_port}
        if not ports:
            LOG.debug(
                "FlowInstaller: flood packet-out dpid=%s in=%d → NO PORTS",
                hex(dp.id),
                in_port,
            )
            return
        actions = [ofp_parser.OFPActionOutput(p) for p in sorted(ports)]
        out = ofp_parser.OFPPacketOut(
            datapath=dp,
            buffer_id=buffer_id,
            in_port=in_port,
            actions=actions,
            data=data if buffer_id == ofp.OFP_NO_BUFFER else None,
        )
        dp.send_msg(out)
        LOG.debug(
            "FlowInstaller: flood packet-out dpid=%s in=%d → ports=%s",
            hex(dp.id),
            in_port,
            sorted(ports),
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
        actions,
        priority: int,
        idle_timeout: int,
        hard_timeout: int,
        cookie: int = 0,
    ) -> None:
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        inst = [ofp_parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        msg = ofp_parser.OFPFlowMod(
            datapath=dp,
            cookie=cookie,
            match=match,
            instructions=inst,
            priority=priority,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            buffer_id=ofp.OFP_NO_BUFFER,
        )
        dp.send_msg(msg)

    def _find_edge_port(self, dpid: int, src_mac: str, dst_mac: str) -> Optional[int]:
        """Find the edge port on *dpid* for the destination host."""
        ht = self._host_tracker
        if ht is not None:
            loc = ht.lookup(dst_mac)
            if loc and loc.dpid == dpid:
                LOG.debug(
                    "FlowInstaller: edge port for %s on dpid=%s → port=%d (from tracker)",
                    dst_mac,
                    hex(dpid),
                    loc.port,
                )
                return loc.port
        # Fallback: pick any edge port on this switch
        for sw, port in self.graph.edge_ports:
            if sw == dpid:
                LOG.debug(
                    "FlowInstaller: edge port for %s on dpid=%s → port=%d (fallback)",
                    dst_mac,
                    hex(dpid),
                    port,
                )
                return port
        LOG.warning(
            "FlowInstaller: no edge port found for %s on dpid=%s", dst_mac, hex(dpid)
        )
        return None
