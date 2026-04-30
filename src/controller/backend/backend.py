"""Backend — os-ken entry point, event dispatch only.

Wires all modules together and delegates os-ken events to the appropriate handler.
No business logic lives here.

Do NOT run this file directly. Use ``run.py`` instead, which calls
eventlet.monkey_patch() early in the startup sequence.
"""

from __future__ import annotations

import logging

import eventlet

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from os_ken.controller.controller import Datapath
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import arp as arp_packet
from os_ken.lib.packet import ethernet, ipv4, packet
from os_ken.topology import event as topo_event

from topology import TopologyGraph, TopologyManager, LinkKey
from host_tracker import HostTracker
from path_computer import PathComputer
from route_tracker import RouteTracker
from flow_installer import FlowInstaller
from switch_registry import SwitchRegistry
from forwarding_plane import ForwardingPlane
from fault_handler import FaultHandler
from policy_manager import PolicyManager
from stats_collector import StatsCollector
from rest_api import RestAPI
from event_logger import LogStore, EventCounters

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

        self.counters = EventCounters()

        self.graph = TopologyGraph()
        self.topo_mgr = TopologyManager(self.graph, counters=self.counters)
        self.host_tracker = HostTracker(counters=self.counters)
        self.path_computer = PathComputer(self.graph)
        self.route_tracker = RouteTracker()
        self.flow_installer = FlowInstaller(self.graph)
        self.switch_registry = SwitchRegistry()
        self.flow_installer.set_registry(self.switch_registry)

        # PolicyManager — must exist before ForwardingPlane / FaultHandler
        self.policy_mgr = PolicyManager(
            flow_installer=self.flow_installer,
            host_tracker=self.host_tracker,
            route_tracker=self.route_tracker,
        )

        self.forwarding = ForwardingPlane(
            self.path_computer,
            self.route_tracker,
            self.flow_installer,
            self.host_tracker,
            self.policy_mgr,
        )
        self.fault_handler = FaultHandler(
            self.graph,
            self.topo_mgr,
            self.forwarding,
            self.flow_installer,
            self.policy_mgr,
        )

        # LogStore — ring-buffer handler that captures all logs for the REST API
        self.log_store = LogStore()
        fmt = logging.Formatter(
            "%(message)s",
        )
        self.log_store.setFormatter(fmt)
        logging.getLogger().addHandler(self.log_store)

        # StatsCollector — periodic port counter polling
        self.stats_collector = StatsCollector(poll_interval=5.0)
        self.stats_collector.set_datapaths_cb(
            lambda: list(self.flow_installer.datapaths.values())
        )

        # RestAPI — user-facing HTTP interface
        self.rest_api = RestAPI(
            graph=self.graph,
            host_tracker=self.host_tracker,
            path_computer=self.path_computer,
            route_tracker=self.route_tracker,
            policy_mgr=self.policy_mgr,
            stats_collector=self.stats_collector,
            log_store=self.log_store,
            counters=self.counters,
            switch_registry=self.switch_registry,
        )

        # Track which switches have had their ports registered.
        self._ports_initialized: set[int] = set()

        # DESC classification fallback timers (dpid → greenthread)
        self._desc_fallbacks: dict[int, eventlet.GreenThread] = {}

        # Launch background services once the os-ken event loop starts
        eventlet.spawn_after(0.0, self._start_services)

        LOG.info("Backend ready — waiting for switches to connect")
        LOG.info("=" * 60)

    # ── Switch lifecycle ─────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev) -> None:
        """Called when a switch connects. Install baseline rules and register switch."""
        dp = ev.msg.datapath
        LOG.info(">>> SWITCH CONNECTED dpid=%s | features received", hex(dp.id))

        self.counters.increment_switch_connected()
        self.flow_installer.register_dp(dp)
        self.graph.add_switch(dp.id)

        # Try to register ports now (usually empty at CONFIG time).
        # If empty, they will be lazily initialized on first packet-in.
        self._try_init_ports(dp)

        self._send_desc_request(dp)

        # Fallback: if no DESC reply within 2 s, install baseline as default
        self._desc_fallbacks[dp.id] = eventlet.spawn_after(
            2.0, self._desc_fallback, dp.id
        )

        self.path_computer.invalidate()
        LOG.info("<<< SWITCH REGISTERED dpid=%s", hex(dp.id))

    @set_ev_cls(
        ofp_event.EventOFPStateChange,
        [MAIN_DISPATCHER, CONFIG_DISPATCHER, DEAD_DISPATCHER],
    )
    def _state_change_handler(self, ev) -> None:
        """Handle switch state transitions and disconnection."""
        dp = ev.datapath
        if ev.state != DEAD_DISPATCHER:
            return
        dpid = dp.id

        # Guard against stale disconnect events
        current_dp = self.flow_installer.get_dp(dpid)
        if current_dp is not None and current_dp is not dp:
            LOG.debug(
                ">>> STALE DISCONNECT dpid=%s — newer connection already active",
                hex(dpid),
            )
            return

        LOG.warning(">>> SWITCH DISCONNECTED dpid=%s — cleaning up", hex(dpid))

        self.counters.increment_switch_disconnected()

        self._cancel_desc_fallback(dpid)
        self.flow_installer.unregister_dp(dpid)
        self.switch_registry.remove(dpid)

        # Purge routes that involved this switch and delete orphaned flows
        purged = self.route_tracker.purge_switch(dpid)
        for src_mac, dst_mac in purged:
            for surviving_dpid in self.graph.switches:
                if surviving_dpid != dpid:
                    self.flow_installer.delete_flows_for_mac(surviving_dpid, dst_mac)
                    self.flow_installer.delete_flows_for_mac(surviving_dpid, src_mac)
            self.policy_mgr.mark_broken(src_mac, dst_mac)

        # Purge hosts that were attached to the dead switch
        removed_hosts = []
        for mac, loc in list(self.host_tracker.hosts.items()):
            if loc.dpid == dpid:
                self.host_tracker.remove_by_port(dpid, loc.port)
                removed_hosts.append(mac)
        if removed_hosts:
            LOG.info(
                "Switch disconnect: purged %d host entries: %s",
                len(removed_hosts),
                ", ".join(removed_hosts),
            )
            # Mark all policies involving purged hosts as BROKEN
            for mac in removed_hosts:
                self.policy_mgr.mark_all_for_mac_broken(mac)

        self.topo_mgr.switch_leave(dp)
        self._ports_initialized.discard(dpid)
        self.path_computer.invalidate()
        LOG.info(
            "<<< Switch dpid=%s removed | purged %d routes, %d hosts",
            hex(dpid),
            len(purged),
            len(removed_hosts),
        )

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
            LOG.warning(
                ">>> PORT DELETED dpid=%s port=%d — triggering fault handler",
                hex(dp.id),
                port_no,
            )
            self.fault_handler.handle_port_down(dp.id, port_no)
        elif reason == ofp.OFPPR_ADD:
            LOG.info(">>> PORT ADDED dpid=%s port=%d", hex(dp.id), port_no)
            self.topo_mgr.port_add(dp, port_no)
        elif reason == ofp.OFPPR_MODIFY:
            is_down = bool(ev.msg.desc.state & ofp.OFPPS_LINK_DOWN)
            LOG.info(
                ">>> PORT MODIFY dpid=%s port=%d state=%s",
                hex(dp.id),
                port_no,
                "DOWN" if is_down else "UP",
            )
            if is_down:
                self.fault_handler.handle_port_down(dp.id, port_no)
            else:
                self.topo_mgr.port_modify(dp, port_no, is_down)
        else:
            LOG.debug(
                "PortStatus: unknown reason=%d dpid=%s port=%d",
                reason,
                hex(dp.id),
                port_no,
            )

    # ── Packet-in (data plane) ───────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev) -> None:
        """Handle a packet that missed in the flow table.

        Three code paths:

        1. **ARP Request** → Proxy ARP (answer directly if target is known,
           silently drop otherwise).

        2. **ARP Reply / Gratuitous ARP** → Learn IPs, drop silently
           (os-ken already handled MAC learning).

        3. **IPv4 Unicast** → Reactive forwarding (install path and forward
           if destination is known, drop otherwise).

        4. **Anything else** → Drop silently (default deny / zero-trust).
        """
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]

        # Lazily initialize ports on first packet-in.
        self._try_init_ports(dp)

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        src_mac = eth.src
        dst_mac = eth.dst

        # LLDP packets are handled by os-ken's built-in switches app
        if dst_mac == LLDP_MAC:
            return

        # ── Learn source host from packet ─────────────────────────────
        # os-ken's host_discovery_packet_in_handler already detected the
        # host location and enqueued an EventHostMove/EventHostAdd *before*
        # this handler runs.  That event has NOT been dispatched yet, so
        # our HostTracker may still be blind to this host (e.g. after an
        # edge-port purge).  Learn it here synchronously so the rest of
        # this handler (add_ip, forwarding) sees the current location.
        # We also learn on a known-internal port whose link is currently
        # down — the host may have moved to it (mobility scenario).
        if not (src_mac == "ff:ff:ff:ff:ff:ff" or int(src_mac[:2], 16) & 1):
            if not self.graph.is_port_connected(dpid, in_port):
                self.host_tracker.add_host(src_mac, dpid, in_port)

        # ── ARP processing ───────────────────────────────────────────
        arp = pkt.get_protocol(arp_packet.arp)
        if arp is not None:
            # Learn source IP: arp.src_ip belongs to eth.src (the sender)
            if arp.src_ip and arp.src_ip != "0.0.0.0":
                self.host_tracker.add_ip(src_mac, arp.src_ip)

            if arp.opcode == arp_packet.ARP_REPLY:
                # In ARP Reply, arp.dst_ip is the original requester's IP
                # and eth.dst is the requester's MAC — learn this association.
                if arp.dst_ip and arp.dst_ip != "0.0.0.0":
                    self.host_tracker.add_ip(dst_mac, arp.dst_ip)
            elif arp.opcode == arp_packet.ARP_REQUEST:
                # Skip gratuitous ARP (src_ip == dst_ip) — already learned above
                if arp.src_ip == arp.dst_ip:
                    return
                self._handle_arp_request(dp, in_port, msg.data, msg.buffer_id, arp, eth)
            return

        # ── IPv4 unicast (reactive forwarding) ────────────────────────
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt is not None:
            # Learn source IP from IPv4 packet
            if ip_pkt.src and ip_pkt.src != "0.0.0.0":
                self.host_tracker.add_ip(src_mac, ip_pkt.src)

        # ── Path 1: broadcast / multicast → drop (zero-trust) ─────────
        # The only exception is handled above (ARP processing).
        # All other broadcast/multicast traffic is silently destroyed.
        if dst_mac == "ff:ff:ff:ff:ff:ff" or int(dst_mac[:2], 16) & 1:
            self.counters.increment_packets_dropped()
            LOG.debug(
                "PacketIn: broadcast/multicast %s → %s on dpid=%s — dropping (zero-trust)",
                src_mac,
                dst_mac,
                hex(dpid),
            )
            return

        # ── Path 2 / 3: unicast forwarding ────────────────────────────
        LOG.info(
            "PacketIn: UNICAST %s → %s on dpid=%s port=%d",
            src_mac,
            dst_mac,
            hex(dpid),
            in_port,
        )
        installed = self.forwarding.handle_packet(src_mac, dst_mac, dpid, in_port)

        if installed:
            dst_loc = self.host_tracker.lookup(dst_mac)
            if dst_loc:
                if dpid == dst_loc.dpid:
                    out_port = dst_loc.port
                else:
                    out_port = self.forwarding.get_output_port(dpid, dst_loc.dpid)
                if out_port is not None:
                    LOG.info(
                        "PacketIn: forwarding first packet %s → %s out port %d",
                        src_mac,
                        dst_mac,
                        out_port,
                    )
                    self.flow_installer.send_packet_out(
                        dp, msg.data, msg.buffer_id, in_port, out_port
                    )
                    self.counters.increment_packets_forwarded()
                    return
            self.counters.increment_packets_dropped()
            LOG.warning(
                "PacketIn: path installed but couldn't find output port — dropping"
            )
        else:
            self.counters.increment_packets_dropped()
            LOG.info(
                "PacketIn: path NOT installed for %s → %s (unknown dst) — dropping",
                src_mac,
                dst_mac,
            )

    # ── Proxy ARP ────────────────────────────────────────────────────

    def _handle_arp_request(
        self,
        dp: Datapath,
        in_port: int,
        data: bytes,
        buffer_id: int,
        arp: any,
        eth: any,
    ) -> None:
        """Handle an ARP Request: reply on behalf of known hosts.

        If the target IP is known, craft an ARP Reply and send it to the
        requester.  If unknown, drop silently (zero-trust).
        """
        target_ip = arp.dst_ip
        src_ip = arp.src_ip
        src_mac = arp.src_mac

        LOG.info(
            "ARP Request: %s (%s) asks for %s on dpid=%s port=%d",
            src_ip,
            src_mac,
            target_ip,
            hex(dp.id),
            in_port,
        )

        # Look up the target in our host table
        result = self.host_tracker.lookup_by_ip(target_ip)
        if result is None:
            LOG.info("ARP Request: target %s UNKNOWN — dropping", target_ip)
            return

        target_mac, target_dpid, target_port = result
        LOG.info(
            "ARP Request: target %s → %s (dpid=%s port=%d) — proxying reply",
            target_ip,
            target_mac,
            hex(target_dpid),
            target_port,
        )

        # Craft ARP Reply
        reply_ether = ethernet.ethernet(
            dst=src_mac,
            src=target_mac,
            ethertype=eth.ethertype,
        )
        reply_arp = arp_packet.arp(
            opcode=arp_packet.ARP_REPLY,
            src_mac=target_mac,
            src_ip=target_ip,
            dst_mac=src_mac,
            dst_ip=src_ip,
        )
        reply_pkt = packet.Packet()
        reply_pkt.add_protocol(reply_ether)
        reply_pkt.add_protocol(reply_arp)
        reply_pkt.serialize()

        # Send the ARP Reply back to the requester
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser
        actions = [ofp_parser.OFPActionOutput(in_port)]
        out = ofp_parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=actions,
            data=reply_pkt.data,
        )
        dp.send_msg(out)
        self.counters.increment_arp_replies_sent()
        LOG.info("ARP Reply: sent %s is at %s to %s", target_ip, target_mac, src_mac)

    # ── Host discovery events (from os-ken) ──────────────────────────

    @set_ev_cls(topo_event.EventHostAdd)
    def _host_add_handler(self, ev) -> None:
        """os-ken discovered a new host. Record its location."""
        host = ev.host
        dpid = host.port.dpid
        port_no = host.port.port_no
        LOG.info(
            ">>> HOST ADD %s → dpid=%s port=%d",
            host.mac,
            hex(dpid),
            port_no,
        )
        self.host_tracker.add_host(host.mac, dpid, port_no)

    @set_ev_cls(topo_event.EventHostMove)
    def _host_move_handler(self, ev) -> None:
        """os-ken detected a host moving to a new port."""
        old_host = ev.src
        new_host = ev.dst
        old_dpid = old_host.port.dpid
        old_port = old_host.port.port_no
        new_dpid = new_host.port.dpid
        new_port = new_host.port.port_no

        LOG.warning(
            ">>> HOST MOVE %s: dpid=%s:%d → dpid=%s:%d",
            old_host.mac,
            hex(old_dpid),
            old_port,
            hex(new_dpid),
            new_port,
        )

        # Update our host tracker
        self.host_tracker.add_host(new_host.mac, new_dpid, new_port)

        # Purge route tracker entries and clean flows in BOTH directions
        purged = self.route_tracker.purge_mac(new_host.mac)
        dpids = self.graph.switches
        for pair in purged:
            for dpid in dpids:
                self.flow_installer.delete_flows_for_mac(dpid, pair[0])
                self.flow_installer.delete_flows_for_mac(dpid, pair[1])
            self.policy_mgr.mark_broken(pair[0], pair[1])

        # Also delete any stale flows targeting the moved host's MAC
        # (catches flows not tracked by route_tracker, e.g. same-switch)
        for dpid in dpids:
            self.flow_installer.delete_flows_for_mac(dpid, new_host.mac)

        # Also mark policies involving the moved MAC as broken
        broken_pairs = self.policy_mgr.mark_all_for_mac_broken(new_host.mac)
        for src_mac, dst_mac in broken_pairs:
            LOG.info(
                "HOST MOVE: policy %s → %s → BROKEN",
                src_mac,
                dst_mac,
            )

        # Invalidate path cache — a host move can affect many cached paths
        self.path_computer.invalidate()

        LOG.info("<<< HOST MOVE %s: stale flows cleaned", new_host.mac)

    # ── Topology events (from os-ken LLDP) ───────────────────────────

    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add_handler(self, ev) -> None:
        """Handle a newly discovered switch-to-switch link (LLDP).

        After adding the link, we clean any hosts that were wrongly learned
        on these ports.  During startup, all ports start as assumed-edge,
        and broadcast traffic can cause the host tracker to absorb source
        MACs on internal ports.  Once LLDP confirms these ports are
        switch-to-switch, any hosts learned there are stale and must be
        purged so they can be re-learned on their true edge ports.
        """
        # Ensure both switches have ports initialized before processing links
        src_dp = self.flow_installer.get_dp(ev.link.src.dpid)
        dst_dp = self.flow_installer.get_dp(ev.link.dst.dpid)
        if src_dp:
            self._try_init_ports(src_dp)
        if dst_dp:
            self._try_init_ports(dst_dp)

        LOG.info(
            ">>> LINK ADD %s:%d → %s:%d (from LLDP)",
            hex(ev.link.src.dpid),
            ev.link.src.port_no,
            hex(ev.link.dst.dpid),
            ev.link.dst.port_no,
        )
        self.topo_mgr.link_add(ev.link)

        removed_src = self.host_tracker.remove_by_port(
            ev.link.src.dpid, ev.link.src.port_no
        )
        removed_dst = self.host_tracker.remove_by_port(
            ev.link.dst.dpid, ev.link.dst.port_no
        )
        if removed_src or removed_dst:
            LOG.info(
                "Link add: cleaned stale hosts on internal ports: %s:%d→%s, %s:%d→%s",
                hex(ev.link.src.dpid),
                ev.link.src.port_no,
                removed_src,
                hex(ev.link.dst.dpid),
                ev.link.dst.port_no,
                removed_dst,
            )

        self.path_computer.invalidate()
        LOG.info("<<< Topology updated — path cache invalidated")

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_delete_handler(self, ev) -> None:
        """Handle a timed-out switch-to-switch link (LLDP)."""
        LOG.warning(
            ">>> LINK DELETE %s:%d → %s:%d (from LLDP) — triggering fault handler",
            hex(ev.link.src.dpid),
            ev.link.src.port_no,
            hex(ev.link.dst.dpid),
            ev.link.dst.port_no,
        )
        lk = LinkKey(
            ev.link.src.dpid,
            ev.link.src.port_no,
            ev.link.dst.dpid,
            ev.link.dst.port_no,
        )
        self.fault_handler.handle_link_down(lk)
        LOG.info("<<< Link failure handled")

    # ── Port stats reply ─────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev) -> None:
        """Delegate port stats replies to StatsCollector."""
        self.stats_collector.on_stats_reply(ev.msg)

    # ── Port initialization helper ───────────────────────────────────

    def _try_init_ports(self, dp: Datapath) -> bool:
        """Register switch ports from dp.ports if not already done.

        Returns True if ports were initialized in this call.
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
        self.switch_registry.set_num_ports(dpid, port_count)
        LOG.info(
            "Port init: dpid=%s registered %d ports from dp.ports: %s",
            hex(dpid),
            port_count,
            sorted(p.port_no for p in dp.ports.values() if p.port_no < 0xFFFFFFF0),
        )
        return True

    # ── Switch classification & baseline install ──────────────────────

    def _send_desc_request(self, dp: Datapath) -> None:
        req = dp.ofproto_parser.OFPDescStatsRequest(dp)
        dp.send_msg(req)
        LOG.debug("Backend: sent OFPMP_DESC to dpid=%s", hex(dp.id))

    @set_ev_cls(ofp_event.EventOFPDescStatsReply, MAIN_DISPATCHER)
    def _desc_reply_handler(self, ev) -> None:
        body = ev.msg.body
        dp = ev.msg.datapath
        LOG.info(
            "Backend: DESC reply dpid=%s "
            "mfr=%s hw=%s sw=%s serial=%s",
            hex(dp.id),
            body.mfr_desc,
            body.hw_desc,
            body.sw_desc,
            body.serial_num,
        )
        self.switch_registry.register(dp.id, body)
        self._try_init_ports(dp)  # re-try: dp.ports is populated by now
        self._install_baseline(dp)
        self._cancel_desc_fallback(dp.id)

    def _desc_fallback(self, dpid: int) -> None:
        self._desc_fallbacks.pop(dpid, None)
        if self.switch_registry.get(dpid) is not None:
            return  # already classified
        self.switch_registry.set_unknown(dpid)
        dp = self.flow_installer.get_dp(dpid)
        if dp:
            self._try_init_ports(dp)
            self._install_baseline(dp)
        LOG.info("Backend: DESC fallback for dpid=%s — using default", hex(dpid))

    def _cancel_desc_fallback(self, dpid: int) -> None:
        gt = self._desc_fallbacks.pop(dpid, None)
        if gt is not None:
            gt.cancel()

    def _install_baseline(self, dp: Datapath) -> None:
        self.flow_installer.install_table_miss(dp)
        self.flow_installer.install_drop_rules(dp)

    def _start_services(self) -> None:
        """Launch background services from the os-ken event loop."""
        LOG.info("Backend: launching background services")
        eventlet.spawn_after(0.0, self.stats_collector._poll_loop)
        self.rest_api.start(host="0.0.0.0", port=8080)
        LOG.info("Backend: background services started")
