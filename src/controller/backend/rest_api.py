"""RestAPI — FastAPI HTTP endpoints for topology, path, stats, and policy management.

Runs in a dedicated thread via ``uvicorn.run()``, sharing state with the
controller through direct object references.  All reads are protected by
the relevant module's ``threading.Lock``.  No live OpenFlow interaction
ever happens from the FastAPI thread.

IMPORTANT: FastAPI and uvicorn are imported lazily (inside methods) to
avoid clashing with eventlet's monkey-patch, which can corrupt asyncio's
event loop if FastAPI is imported at module level in the main thread.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI
    from stats_collector import StatsCollector

from topology import LinkKey, TopologyGraph
from host_tracker import HostTracker
from path_computer import PathComputer
from route_tracker import RouteTracker
from policy_manager import PolicyManager, PolicyState

LOG = logging.getLogger(__name__)


def _validate_mac_404(entity: Any, mac: str, label: str) -> None:
    """Raise 404 if *entity* is None (MAC not known to HostTracker)."""
    if entity is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=404,
            detail=f"{label} {mac} is unknown to HostTracker",
        )


def _build_hops(
    dpids: list[int],
    links: list[LinkKey],
    graph: TopologyGraph,
    host_tracker: HostTracker,
    src_mac: str,
    dst_mac: str,
) -> list[dict[str, int]]:
    """Convert a dpid-path + link list into the hops format for GET /path."""
    if len(dpids) == 1:
        src_loc = host_tracker.lookup(src_mac)
        dst_loc = host_tracker.lookup(dst_mac)
        in_p = src_loc.port if src_loc else 0
        out_p = dst_loc.port if dst_loc else 0
        return [{"dpid": dpids[0], "in_port": in_p, "out_port": out_p}]

    hops: list[dict[str, int]] = []
    for i, dpid in enumerate(dpids):
        if i == 0:
            src_loc = host_tracker.lookup(src_mac)
            in_p = src_loc.port if src_loc else 0
            out_p = graph.get_port_for_peer(dpid, dpids[1]) or 0
        elif i == len(dpids) - 1:
            in_p = graph.get_port_for_peer(dpid, dpids[i - 1]) or 0
            dst_loc = host_tracker.lookup(dst_mac)
            out_p = dst_loc.port if dst_loc else 0
        else:
            in_p = graph.get_port_for_peer(dpid, dpids[i - 1]) or 0
            out_p = graph.get_port_for_peer(dpid, dpids[i + 1]) or 0
        hops.append({"dpid": dpid, "in_port": in_p, "out_port": out_p})
    return hops


class RestAPI:
    """User-facing HTTP interface for the SDN controller.

    Start with ``rest_api.start(host, port)`` (called in a dedicated thread).
    Stop with ``rest_api.stop()``.

    FastAPI/uvicorn are imported and built lazily inside ``start()`` to avoid
    asyncio/eventlet clashes in the main thread.
    """

    def __init__(
        self,
        graph: TopologyGraph,
        host_tracker: HostTracker,
        path_computer: PathComputer,
        route_tracker: RouteTracker,
        policy_mgr: PolicyManager,
        stats_collector: "StatsCollector",
    ) -> None:
        """Store references to all controller modules.

        The FastAPI app is NOT built here — it is constructed lazily
        inside ``start()`` to keep FastAPI/uvicorn imports out of the
        module-level scope (avoiding asyncio/eventlet import clashes).
        """
        self._graph = graph
        self._host_tracker = host_tracker
        self._path_computer = path_computer
        self._route_tracker = route_tracker
        self._policy_mgr = policy_mgr
        self._stats_collector = stats_collector

        self._app: Optional["FastAPI"] = None
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None

    @property
    def app(self) -> "FastAPI":
        """Return the FastAPI application (only valid after ``start()``)."""
        if self._app is None:
            raise RuntimeError("RestAPI.start() must be called before accessing .app")
        return self._app

    def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Build the FastAPI app and launch uvicorn in a dedicated daemon thread."""
        import uvicorn
        from fastapi import FastAPI

        self._app = FastAPI(title="SDN Controller REST API", version="0.2.0")

        from fastapi.middleware.cors import CORSMiddleware

        self._app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        self._register_routes()

        config = uvicorn.Config(
            app=self._app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        def _run() -> None:
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._server.serve())  # type: ignore[union-attr]

        self._thread = threading.Thread(target=_run, daemon=True, name="uvicorn")
        self._thread.start()
        LOG.info("RestAPI: started on %s:%d", host, port)

    def stop(self) -> None:
        """Signal uvicorn to shut down."""
        if self._server is not None:
            self._server.should_exit = True
            LOG.info("RestAPI: shutdown signaled")

    # ── Route registration ───────────────────────────────────────────

    def _register_routes(self) -> None:
        """Build all REST endpoints on the FastAPI app.

        Called once from ``start()`` after the FastAPI instance is created.
        Route handler closures capture ``api`` (self) to access controller
        state — no global variables, no IPC.
        """
        from fastapi.responses import JSONResponse

        app = self._app
        assert app is not None
        api = self  # capture for closures

        # ── 1. GET /path/{src_mac}/{dst_mac} ──────────────────────────

        @app.get("/path/{src_mac}/{dst_mac}")
        def get_path(src_mac: str, dst_mac: str):
            src_loc = api._host_tracker.lookup(src_mac)
            dst_loc = api._host_tracker.lookup(dst_mac)
            _validate_mac_404(src_loc, src_mac, "SRC")
            _validate_mac_404(dst_loc, dst_mac, "DST")

            # Check policy plane first — both directions
            entry = api._policy_mgr.get_policy(src_mac, dst_mac)
            is_reverse = False
            if entry is None:
                entry = api._policy_mgr.get_policy(dst_mac, src_mac)
                is_reverse = True

            if entry is not None and entry.state in (
                PolicyState.POLICY_ACTIVE,
                PolicyState.POLICY_BROKEN,
            ):
                if is_reverse:
                    rev_links = [lk.reverse for lk in reversed(entry.path)]
                    dpids: list[int] = []
                    if rev_links:
                        dpids = [rev_links[0].src_dpid]
                        for lk in rev_links:
                            dpids.append(lk.dst_dpid)
                    return JSONResponse(
                        content={
                            "src_mac": src_mac,
                            "dst_mac": dst_mac,
                            "plane": "policy",
                            "state": entry.state.value,
                            "hops": _build_hops(
                                dpids,
                                rev_links,
                                api._graph,
                                api._host_tracker,
                                src_mac,
                                dst_mac,
                            ),
                        }
                    )
                else:
                    dpids: list[int] = []
                    if entry.path:
                        dpids = [entry.path[0].src_dpid]
                        for lk in entry.path:
                            dpids.append(lk.dst_dpid)
                    return JSONResponse(
                        content={
                            "src_mac": src_mac,
                            "dst_mac": dst_mac,
                            "plane": "policy",
                            "state": entry.state.value,
                            "hops": _build_hops(
                                dpids,
                                entry.path,
                                api._graph,
                                api._host_tracker,
                                src_mac,
                                dst_mac,
                            ),
                        }
                    )

            # Default plane: check RouteTracker then PathComputer
            pair_links = api._route_tracker.links_for_pair(src_mac, dst_mac)
            dpids: list[int] = []
            plane: str
            state: str
            if pair_links:
                dpids = [pair_links[0].src_dpid]
                for lk in pair_links:
                    dpids.append(lk.dst_dpid)
                plane = "default"
                state = "active"
            else:
                src_dpid = src_loc.dpid
                dst_dpid = dst_loc.dpid
                path = api._path_computer.compute_path(src_dpid, dst_dpid)
                if path is None:
                    dpids = []
                    plane = "default"
                    state = "unspecified"
                else:
                    dpids = path
                    plane = "default"
                    state = "unspecified"

            return JSONResponse(
                content={
                    "src_mac": src_mac,
                    "dst_mac": dst_mac,
                    "plane": plane,
                    "state": state,
                    "hops": _build_hops(
                        dpids,
                        pair_links,
                        api._graph,
                        api._host_tracker,
                        src_mac,
                        dst_mac,
                    ),
                }
            )

        # ── 2. GET /stats/ports ───────────────────────────────────────

        @app.get("/stats/ports")
        def get_stats_ports():
            from fastapi import HTTPException

            if not api._stats_collector.has_data:
                raise HTTPException(
                    status_code=503,
                    detail="StatsCollector has not yet received any reply from the switches",
                )

            snapshot = api._stats_collector.get_snapshot()
            switches = []
            for dpid in sorted(snapshot):
                ports = []
                for port_no in sorted(snapshot[dpid]):
                    ps = snapshot[dpid][port_no]
                    ports.append(
                        {
                            "port_no": ps.port_no,
                            "rx_packets": ps.rx_packets,
                            "tx_packets": ps.tx_packets,
                            "rx_bytes": ps.rx_bytes,
                            "tx_bytes": ps.tx_bytes,
                            "rx_dropped": ps.rx_dropped,
                            "tx_dropped": ps.tx_dropped,
                            "rx_errors": ps.rx_errors,
                            "tx_errors": ps.tx_errors,
                            "last_updated": ps.last_updated,
                        }
                    )
                switches.append({"dpid": dpid, "ports": ports})
            return JSONResponse(content={"switches": switches})

        # ── 3. GET /flows ──────────────────────────────────────────────

        @app.get("/flows")
        def get_flows():
            entries: list[dict] = []
            policy_snapshot = api._policy_mgr.all_entries

            # Default-plane flows from RouteTracker
            for (src, dst), links in api._route_tracker.all_routes.items():
                if (src, dst) in policy_snapshot:
                    continue
                dst_loc = api._host_tracker.lookup(dst)
                for lk in links:
                    out_port = lk.src_port
                    if dst_loc and lk.src_dpid == dst_loc.dpid:
                        out_port = dst_loc.port
                    entries.append(
                        {
                            "dpid": lk.src_dpid,
                            "match": {"eth_dst": dst},
                            "out_port": out_port,
                            "priority": 10,
                            "idle_timeout": 30,
                            "plane": "default",
                            "src_mac": src,
                            "dst_mac": dst,
                        }
                    )
                # Sink switch flow (last link's dst_dpid → host edge port)
                if links and dst_loc:
                    sink_dpid = links[-1].dst_dpid
                    if not any(e["dpid"] == sink_dpid for e in entries[-len(links) :]):
                        entries.append(
                            {
                                "dpid": sink_dpid,
                                "match": {"eth_dst": dst},
                                "out_port": dst_loc.port,
                                "priority": 10,
                                "idle_timeout": 30,
                                "plane": "default",
                                "src_mac": src,
                                "dst_mac": dst,
                            }
                        )

            # Policy-plane flows
            for (src, dst), pol in policy_snapshot.items():
                if pol.state != PolicyState.POLICY_ACTIVE:
                    continue
                dst_loc = api._host_tracker.lookup(dst)
                for lk in pol.path:
                    out_port = lk.src_port
                    if dst_loc and lk.src_dpid == dst_loc.dpid:
                        out_port = dst_loc.port
                    entries.append(
                        {
                            "dpid": lk.src_dpid,
                            "match": {"eth_dst": dst},
                            "out_port": out_port,
                            "priority": 20,
                            "idle_timeout": 0,
                            "plane": "policy",
                            "src_mac": src,
                            "dst_mac": dst,
                        }
                    )
                if pol.path and dst_loc:
                    sink_dpid = pol.path[-1].dst_dpid
                    if not any(
                        e["dpid"] == sink_dpid and e["plane"] == "policy"
                        for e in entries[-len(pol.path) :]
                    ):
                        entries.append(
                            {
                                "dpid": sink_dpid,
                                "match": {"eth_dst": dst},
                                "out_port": dst_loc.port,
                                "priority": 20,
                                "idle_timeout": 0,
                                "plane": "policy",
                                "src_mac": src,
                                "dst_mac": dst,
                            }
                        )

            return JSONResponse(content={"flows": entries})

        # ── 4. GET /topology ──────────────────────────────────────────

        @app.get("/topology")
        def get_topology():
            with api._graph.lock:
                switches = sorted(api._graph.switches)
                raw_links = list(api._graph.links)

            links = [
                {
                    "src_dpid": lk.src_dpid,
                    "src_port": lk.src_port,
                    "dst_dpid": lk.dst_dpid,
                    "dst_port": lk.dst_port,
                }
                for lk in raw_links
            ]

            hosts = api._host_tracker.get_all_hosts()

            return JSONResponse(
                content={
                    "switches": switches,
                    "links": links,
                    "hosts": hosts,
                }
            )

        # ── 5. GET /policy/{src_mac}/{dst_mac} ────────────────────────

        @app.get("/policy/{src_mac}/{dst_mac}")
        def get_policy(src_mac: str, dst_mac: str):
            src_loc = api._host_tracker.lookup(src_mac)
            dst_loc = api._host_tracker.lookup(dst_mac)
            _validate_mac_404(src_loc, src_mac, "SRC")
            _validate_mac_404(dst_loc, dst_mac, "DST")

            entry = api._policy_mgr.get_policy(src_mac, dst_mac)
            state = entry.state if entry else PolicyState.UNSPECIFIED
            path = None
            if (
                entry is not None
                and entry.state != PolicyState.UNSPECIFIED
                and entry.path
            ):
                path = [
                    {
                        "src_dpid": lk.src_dpid,
                        "src_port": lk.src_port,
                        "dst_dpid": lk.dst_dpid,
                        "dst_port": lk.dst_port,
                    }
                    for lk in entry.path
                ]

            return JSONResponse(
                content={
                    "src_mac": src_mac,
                    "dst_mac": dst_mac,
                    "state": state.value,
                    "path": path,
                }
            )

        # ── 6. POST /policy/{src_mac}/{dst_mac} ───────────────────────

        @app.post("/policy/{src_mac}/{dst_mac}")
        async def post_policy(src_mac: str, dst_mac: str, body: dict):
            from fastapi import HTTPException

            src_loc = api._host_tracker.lookup(src_mac)
            dst_loc = api._host_tracker.lookup(dst_mac)
            _validate_mac_404(src_loc, src_mac, "SRC")
            _validate_mac_404(dst_loc, dst_mac, "DST")

            if src_mac == dst_mac:
                raise HTTPException(
                    status_code=409,
                    detail="SRC and DST are the same host",
                )

            raw_path: list[dict] = body.get("path", [])
            if not raw_path:
                raise HTTPException(
                    status_code=400,
                    detail="Path is empty — provide at least one link",
                )

            path_links, error = api._validate_path(raw_path, src_loc.dpid, dst_loc.dpid)
            if error:
                raise HTTPException(status_code=400, detail=error)

            api._policy_mgr.set_policy(src_mac, dst_mac, path_links)
            return JSONResponse(
                content={
                    "message": f"Policy installed for {src_mac} → {dst_mac}",
                }
            )

        # ── DELETE /policy/{src_mac}/{dst_mac} ────────────────────────

        @app.delete("/policy/{src_mac}/{dst_mac}")
        def delete_policy(src_mac: str, dst_mac: str):
            from fastapi import HTTPException

            src_loc = api._host_tracker.lookup(src_mac)
            dst_loc = api._host_tracker.lookup(dst_mac)
            if src_loc is None or dst_loc is None:
                raise HTTPException(
                    status_code=404,
                    detail="One or both MACs are unknown to HostTracker",
                )

            removed = api._policy_mgr.remove_policy(src_mac, dst_mac)
            if not removed:
                raise HTTPException(
                    status_code=404,
                    detail=f"No active policy for pair {src_mac} → {dst_mac}",
                )
            return JSONResponse(
                content={"message": f"Policy removed for {src_mac} → {dst_mac}"}
            )

    # ── Path validation ──────────────────────────────────────────────

    def _validate_path(
        self,
        raw_path: list[dict],
        src_dpid: int,
        dst_dpid: int,
    ) -> tuple[Optional[list[LinkKey]], Optional[str]]:
        """Validate a raw path dict against the current topology graph.

        Returns (link_list, None) on success or (None, error_message) on failure.
        """
        # Parse raw dicts into LinkKeys
        links: list[LinkKey] = []
        for i, hop in enumerate(raw_path):
            try:
                lk = LinkKey(
                    src_dpid=int(str(hop["src_dpid"]), 0),
                    src_port=int(str(hop["src_port"]), 0),
                    dst_dpid=int(str(hop["dst_dpid"]), 0),
                    dst_port=int(str(hop["dst_port"]), 0),
                )
            except (KeyError, TypeError, ValueError) as exc:
                return None, f"Invalid link at position {i}: {exc}"
            links.append(lk)

        if not links:
            return None, "Path is empty"

        # First link must start from src_dpid
        if links[0].src_dpid != src_dpid:
            return None, (
                f"Path must start from src host switch dpid={hex(src_dpid)}, "
                f"but first link starts from dpid={hex(links[0].src_dpid)}"
            )

        # Last link must end at dst_dpid
        if links[-1].dst_dpid != dst_dpid:
            return None, (
                f"Path must end at dst host switch dpid={hex(dst_dpid)}, "
                f"but last link ends at dpid={hex(links[-1].dst_dpid)}"
            )

        with self._graph.lock:
            # Verify contiguity: link[k].dst_dpid == link[k+1].src_dpid
            for i in range(len(links) - 1):
                if links[i].dst_dpid != links[i + 1].src_dpid:
                    return None, (
                        f"Non-contiguous path at position {i}: "
                        f"link ends at dpid={hex(links[i].dst_dpid)} but "
                        f"next link starts at dpid={hex(links[i + 1].src_dpid)}"
                    )

            # Verify each link exists in the current topology graph
            for i, lk in enumerate(links):
                expected_src = self._graph.get_port_for_peer(lk.src_dpid, lk.dst_dpid)
                expected_dst = self._graph.get_port_for_peer(lk.dst_dpid, lk.src_dpid)
                if expected_src is None or expected_dst is None:
                    return None, (
                        f"Link at position {i} is not in the topology: "
                        f"{hex(lk.src_dpid)}:{lk.src_port} → "
                        f"{hex(lk.dst_dpid)}:{lk.dst_port} "
                        f"(no edge between dpids {hex(lk.src_dpid)} and "
                        f"{hex(lk.dst_dpid)})"
                    )
                if expected_src != lk.src_port:
                    return None, (
                        f"Link at position {i} has wrong src_port: "
                        f"{hex(lk.src_dpid)}:{lk.src_port} → "
                        f"{hex(lk.dst_dpid)}:{lk.dst_port} "
                        f"(expected src_port {expected_src})"
                    )
                if expected_dst != lk.dst_port:
                    return None, (
                        f"Link at position {i} has wrong dst_port: "
                        f"{hex(lk.src_dpid)}:{lk.src_port} → "
                        f"{hex(lk.dst_dpid)}:{lk.dst_port} "
                        f"(expected dst_port {expected_dst})"
                    )

        return links, None
