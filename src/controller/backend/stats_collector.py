"""StatsCollector — periodic OpenFlow port statistics polling via Eventlet greenthread.

Runs inside the os-ken process. Every N seconds (default 5) sends an
``OFPPortStatsRequest`` to every connected datapath. Replies arrive as
``EventOFPPortStatsReply`` events and are handled by ``Backend``, which
delegates to ``on_stats_reply()``. Results are stored in a dict protected
by a ``threading.Lock`` and read directly by the ``RestAPI`` thread — no
live OpenFlow interaction from outside the os-ken event loop.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from os_ken.controller.controller import Datapath
    from os_ken.ofproto.ofproto_v1_3_parser import OFPPortStatsReply

LOG = logging.getLogger(__name__)

# OFPPortStatsReply port numbers don't include the OFPP_* virtual port
# sentinels, so no filtering needed.  We still guard against negative or
# 0xff…f8+ values just in case.

_VIRTUAL_PORT_FLOOR = 0xFFFFFFF8


@dataclass
class PortStats:
    port_no: int
    rx_packets: int = 0
    tx_packets: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    last_updated: float = 0.0


class StatsCollector:
    """Periodically polls all connected switches for port counters.

    Spawned at startup via ``hub.spawn_after(0, collector._poll_loop)``
    inside the os-ken event loop.  Results are stored in a shared dict
    keyed by ``dpid → port_no → PortStats``, protected by ``self.lock``.
    """

    def __init__(self, poll_interval: float = 5.0) -> None:
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._stats: dict[int, dict[int, PortStats]] = {}
        self._datapaths_cb: Optional[Callable[[], list[Datapath]]] = None
        self._received_first_reply = False
        LOG.info("StatsCollector: created (poll_interval=%.1fs)", poll_interval)

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    @property
    def has_data(self) -> bool:
        with self._lock:
            return self._received_first_reply

    def set_datapaths_cb(self, cb: Callable[[], list["Datapath"]]) -> None:
        self._datapaths_cb = cb

    def _poll_loop(self) -> None:
        """Eventlet greenthread body — polls all datapaths periodically."""
        LOG.info(
            "StatsCollector: poll loop started (interval=%.1fs)", self._poll_interval
        )
        while True:
            import eventlet

            eventlet.sleep(self._poll_interval)

            if self._datapaths_cb is None:
                LOG.warning("StatsCollector: no datapaths callback set — skipping poll")
                continue

            datapaths = self._datapaths_cb()
            if not datapaths:
                LOG.debug("StatsCollector: no connected switches — skipping poll")
                continue

            for dp in datapaths:
                try:
                    parser = dp.ofproto_parser
                    req = parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY)
                    dp.send_msg(req)
                except Exception:
                    LOG.exception(
                        "StatsCollector: failed to send stats request to dpid=%s",
                        hex(dp.id),
                    )

    def on_stats_reply(self, msg: "OFPPortStatsReply") -> None:
        """Process an ``OFPPortStatsReply`` from *dp*."""
        try:
            dpid = msg.datapath.id
        except Exception:
            LOG.warning("StatsCollector: stats reply without datapath — ignored")
            return

        now = time.time()
        with self._lock:
            ports = self._stats.setdefault(dpid, {})
            for entry in msg.body:
                port_no = entry.port_no
                if port_no >= _VIRTUAL_PORT_FLOOR:
                    continue
                ports[port_no] = PortStats(
                    port_no=port_no,
                    rx_packets=entry.rx_packets,
                    tx_packets=entry.tx_packets,
                    rx_bytes=entry.rx_bytes,
                    tx_bytes=entry.tx_bytes,
                    rx_dropped=entry.rx_dropped,
                    tx_dropped=entry.tx_dropped,
                    rx_errors=entry.rx_errors,
                    tx_errors=entry.tx_errors,
                    last_updated=now,
                )
            self._received_first_reply = True

        LOG.debug(
            "StatsCollector: received stats for dpid=%s | %d ports",
            hex(dpid),
            len(ports),
        )

    def get_snapshot(self) -> dict[int, dict[int, PortStats]]:
        """Return a snapshot of the current stats (shares ``PortStats`` references).

        The outer dict structure is copied; ``PortStats`` objects themselves
        are shared.  Consumers must treat them as read-only.
        """
        # The RestAPI endpoint will format this.
        with self._lock:
            return {dpid: dict(ports) for dpid, ports in self._stats.items()}
