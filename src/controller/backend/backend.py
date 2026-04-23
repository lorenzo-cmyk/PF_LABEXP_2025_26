"""Backend — os-ken entry point, event dispatch only.

Wires all modules together and delegates os-ken events to the appropriate handler.
No business logic lives here.

Do NOT run this file directly. Use ``run.py`` instead, which ensures
eventlet.monkey_patch() is called before any other imports.
"""

from __future__ import annotations

import logging

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.controller.controller import Datapath
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import ethernet, packet
from os_ken.topology import event as topo_event

from topology import TopologyGraph, TopologyManager, LinkKey
from spanning_tree import SpanningTreeManager
from host_tracker import HostTracker
from path_computer import PathComputer
from route_tracker import RouteTracker
from flow_installer import FlowInstaller
from forwarding_plane import ForwardingPlane
from fault_handler import FaultHandler
from policy_manager import PolicyManager
from rest_api import RestAPI

LOG = logging.getLogger(__name__)

# LLDP destination MAC — os-ken uses this for topology discovery.
LLDP_MAC = "01:80:c2:00:00:0e"


class Backend(app_manager.OSKenApp):
    """os-ken application that dispatches events to controller modules."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        LOG.info("=" * 60)
        LOG.info("Backend initializing — wiring all modules")

        self.graph = TopologyGraph()
        self.topo_mgr = TopologyManager(self.graph)
        self.st_mgr = SpanningTreeManager(self.graph)
        self.host_tracker = HostTracker()
        self.path_computer = PathComputer(self.graph)
        self.route_tracker = RouteTracker()
        self.flow_installer = FlowInstaller(self.graph)
        self.forwarding = ForwardingPlane(
            self.path_computer, self.route_tracker,
            self.flow_installer, self.host_tracker,
        )
        self.fault_handler = FaultHandler(
            self.graph, self.topo_mgr, self.st_mgr,
            self.forwarding, self.flow_installer,
        )
        self.policy_mgr = PolicyManager()  # stub for GOAL 1
        self.rest_api = RestAPI()          # stub for GOAL 1

        # Track which switches have had their ports registered.
        # os-ken's switches app populates dp.ports via EventOFPPortDescStatsReply
        # (which we can't reliably intercept), so we lazily read dp.ports on
        # first packet-in or link event.
        self._ports_initialized: set[int] = set()

        LOG.info("Backend ready — waiting for switches to connect")
        LOG.info("=" * 60)

    # ── Switch lifecycle ─────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev) -> None:
        """Called when a switch connects. Install table-miss and register switch."""
        dp = ev.msg.datapath
        LOG.info(">>> SWITCH CONNECTED dpid=%s | features received", hex(dp.id))

        self.flow_installer.register_dp(dp)
        self.graph.add_switch(dp.id)

        # Try to register ports now (usually empty at CONFIG time).
        # If empty, they will be lazily initialized on first packet-in.
        self._try_init_ports(dp)

        self._install_table_miss(dp)
        LOG.info("<<< SWITCH REGISTERED dpid=%s", hex(dp.id))

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, CONFIG_DISPATCHER])
    def _state_change_handler(self, ev) -> None:
        """Handle switch state transitions and disconnection."""
        dp = ev.datapath
        if ev.state == ofproto_v1_3.OFPCR_ROLE_NOCHANGE:
            return
        if hasattr(ev, 'state') and ev.state == 0:  # DEAD
            dpid = dp.id
            LOG.warning(">>> SWITCH DISCONNECTED dpid=%s — cleaning up", hex(dpid))

            self.flow_installer.unregister_dp(dpid)

            # Purge routes that involved this switch and delete orphaned
            # flows on surviving switches. Port-status events handle the
            # directly-connected links; this catches anything further
            # upstream (e.g., flows on s1 for path s1→s2→s3 when s2 dies).
            purged = self.route_tracker.purge_switch(dpid)
            for src_mac, dst_mac in purged:
                for surviving_dpid in self.graph.switches:
                    if surviving_dpid != dpid:
                        self.flow_installer.delete_flows_for_mac(surviving_dpid, dst_mac)
                        self.flow_installer.delete_flows_for_mac(surviving_dpid, src_mac)

            # Purge hosts that were attached to the dead switch
            removed_hosts = []
            for mac, loc in list(self.host_tracker.hosts.items()):
                if loc.dpid == dpid:
                    self.host_tracker.remove_by_port(dpid, loc.port)
                    removed_hosts.append(mac)
            if removed_hosts:
                LOG.info("Switch disconnect: purged %d host entries: %s",
                         len(removed_hosts), ", ".join(removed_hosts))

            self.topo_mgr.switch_leave(dp)
            self._ports_initialized.discard(dpid)
            self.st_mgr.compute()
            self._install_all_flood_rules()
            LOG.info("<<< Switch dpid=%s removed | purged %d routes, %d hosts",
                     hex(dpid), len(purged), len(removed_hosts))

    # ── Port status ──────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev) -> None:
        """React to port up/down events."""
        dp = ev.msg.datapath
        port_no = ev.msg.desc.port_no
        reason = ev.msg.reason
        ofp = dp.ofproto

        # Ensure ports are initialized before processing port status
        self._try_init_ports(dp)

        if reason == ofp.OFPPR_DELETE:
            LOG.warning(">>> PORT DELETED dpid=%s port=%d — triggering fault handler",
                        hex(dp.id), port_no)
            self.fault_handler.handle_port_down(dp.id, port_no)
        elif reason == ofp.OFPPR_ADD:
            LOG.info(">>> PORT ADDED dpid=%s port=%d", hex(dp.id), port_no)
            self.topo_mgr.port_add(dp, port_no)
        elif reason == ofp.OFPPR_MODIFY:
            is_down = bool(ev.msg.desc.state & ofp.OFPPS_LINK_DOWN)
            LOG.info(">>> PORT MODIFY dpid=%s port=%d state=%s",
                     hex(dp.id), port_no, "DOWN" if is_down else "UP")
            if is_down:
                # Resolve the link BEFORE port_modify removes it from the graph.
                # handle_port_down needs to know which link failed so it can
                # delete affected flows from the switches.
                self.fault_handler.handle_port_down(dp.id, port_no)
            else:
                self.topo_mgr.port_modify(dp, port_no, is_down)
        else:
            LOG.debug("PortStatus: unknown reason=%d dpid=%s port=%d",
                      reason, hex(dp.id), port_no)

    # ── Packet-in (data plane) ───────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev) -> None:
        """Handle a packet that missed in the flow table."""
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]

        # Lazily initialize ports on first packet-in. By this point
        # os-ken's switches app has already populated dp.ports via
        # EventOFPPortDescStatsReply.
        self._try_init_ports(dp)

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            LOG.debug("PacketIn: non-ethernet packet on dpid=%s port=%d — ignoring",
                      hex(dpid), in_port)
            return

        src_mac = eth.src
        dst_mac = eth.dst

        # Silently drop LLDP packets — os-ken's switches app handles these
        if dst_mac == LLDP_MAC:
            return

        # Broadcast / multicast: flood via spanning tree ports only
        if dst_mac == "ff:ff:ff:ff:ff:ff" or int(dst_mac.replace(":", ""), 16) & 1:
            flood_ports = self.st_mgr.flood_ports(dpid)
            LOG.debug("PacketIn: BROADCAST %s → %s on dpid=%s port=%d → flood ports=%s",
                      src_mac, dst_mac, hex(dpid), in_port, sorted(flood_ports))
            self.flow_installer.flood_packet_out(dp, in_port, flood_ports,
                                                  msg.data, msg.buffer_id)
            return

        # Unicast: try to install a path
        LOG.info("PacketIn: UNICAST %s → %s on dpid=%s port=%d",
                 src_mac, dst_mac, hex(dpid), in_port)
        installed = self.forwarding.handle_packet(src_mac, dst_mac, dpid, in_port)

        if installed:
            # Send the first packet out the correct port
            dst_loc = self.host_tracker.lookup(dst_mac)
            if dst_loc:
                out_port = self.forwarding.get_output_port(dpid, dst_loc.dpid)
                if out_port is not None:
                    LOG.info("PacketIn: forwarding first packet %s → %s out port %d",
                             src_mac, dst_mac, out_port)
                    self.flow_installer.send_packet_out(dp, msg.data,
                                                         msg.buffer_id, in_port, out_port)
                    return
            # Fallback: flood if we couldn't determine the output port
            LOG.warning("PacketIn: path installed but couldn't find output port — flooding")
            flood_ports = self.st_mgr.flood_ports(dpid)
            self.flow_installer.flood_packet_out(dp, in_port, flood_ports,
                                                  msg.data, msg.buffer_id)
        else:
            LOG.info("PacketIn: path NOT installed (unknown/unreachable dst %s) — flooding",
                     dst_mac)
            flood_ports = self.st_mgr.flood_ports(dpid)
            self.flow_installer.flood_packet_out(dp, in_port, flood_ports,
                                                  msg.data, msg.buffer_id)

    # ── Topology events (from os-ken LLDP) ───────────────────────────

    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add_handler(self, ev) -> None:
        # Ensure both switches have ports initialized before processing links
        src_dp = self.flow_installer.get_dp(ev.link.src.dpid)
        dst_dp = self.flow_installer.get_dp(ev.link.dst.dpid)
        if src_dp:
            self._try_init_ports(src_dp)
        if dst_dp:
            self._try_init_ports(dst_dp)

        LOG.info(">>> LINK ADD %s:%d → %s:%d (from LLDP)",
                 hex(ev.link.src.dpid), ev.link.src.port_no,
                 hex(ev.link.dst.dpid), ev.link.dst.port_no)
        self.topo_mgr.link_add(ev.link)

        # Clean up any hosts wrongly learned on these ports (before the link
        # was discovered, these ports were assumed edge and may have absorbed
        # flooded traffic with wrong source MACs).
        removed_src = self.host_tracker.remove_by_port(
            ev.link.src.dpid, ev.link.src.port_no)
        removed_dst = self.host_tracker.remove_by_port(
            ev.link.dst.dpid, ev.link.dst.port_no)
        if removed_src or removed_dst:
            LOG.info("Link add: cleaned stale hosts on internal ports: "
                     "%s:%d→%s, %s:%d→%s",
                     hex(ev.link.src.dpid), ev.link.src.port_no, removed_src,
                     hex(ev.link.dst.dpid), ev.link.dst.port_no, removed_dst)

        self.st_mgr.compute()
        self._install_all_flood_rules()
        LOG.info("<<< Topology updated — ST recomputed, flood rules refreshed")

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_delete_handler(self, ev) -> None:
        LOG.warning(">>> LINK DELETE %s:%d → %s:%d (from LLDP) — triggering fault handler",
                     hex(ev.link.src.dpid), ev.link.src.port_no,
                     hex(ev.link.dst.dpid), ev.link.dst.port_no)
        lk = LinkKey(
            ev.link.src.dpid, ev.link.src.port_no,
            ev.link.dst.dpid, ev.link.dst.port_no,
        )
        self.fault_handler.handle_link_down(lk)
        LOG.info("<<< Link failure handled")

    # ── Port initialization helper ───────────────────────────────────

    def _try_init_ports(self, dp: Datapath) -> bool:
        """Register switch ports from dp.ports if not already done.

        Returns True if ports were initialized in this call.
        os-ken's switches app populates dp.ports via EventOFPPortDescStatsReply,
        which happens asynchronously after switch features. We call this
        opportunistically (on first packet-in, first link event, etc.) to
        ensure ports are registered before we need them.
        """
        dpid = dp.id
        if dpid in self._ports_initialized:
            return False

        if not dp.ports:
            LOG.debug("Port init: dpid=%s dp.ports still empty — skipping", hex(dpid))
            return False

        port_count = 0
        for port in dp.ports.values():
            if port.port_no < 0xFFFFFFF0:
                self.graph.add_port(dpid, port.port_no)
                port_count += 1

        self._ports_initialized.add(dpid)
        LOG.info("Port init: dpid=%s registered %d ports from dp.ports: %s",
                 hex(dpid), port_count,
                 sorted(p.port_no for p in dp.ports.values() if p.port_no < 0xFFFFFFF0))

        # Recompute ST and install flood rules now that we have ports
        self.st_mgr.compute()
        self._install_all_flood_rules()
        return True

    # ── Helpers ──────────────────────────────────────────────────────

    def _install_table_miss(self, dp: Datapath) -> None:
        """Install a table-miss entry that sends unmatched packets to the controller."""
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        match = ofp_parser.OFPMatch()
        actions = [ofp_parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                               ofp.OFPCML_NO_BUFFER)]
        inst = [ofp_parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS,
                                                  actions)]
        msg = ofp_parser.OFPFlowMod(
            datapath=dp,
            priority=0,
            match=match,
            instructions=inst,
            buffer_id=ofp.OFP_NO_BUFFER,
        )
        dp.send_msg(msg)
        LOG.info("Backend: installed table-miss on dpid=%s (→ CONTROLLER)", hex(dp.id))

    def _install_all_flood_rules(self) -> None:
        """Install flood rules on all connected switches (idempotent)."""
        switch_count = 0
        for dpid in self.graph.switches:
            flood_ports = self.st_mgr.flood_ports(dpid)
            if flood_ports:
                self.flow_installer.install_flood_rules(dpid, flood_ports)
                switch_count += 1
        LOG.info("Backend: flood rules installed on %d/%d switches",
                 switch_count, len(self.graph.switches))
