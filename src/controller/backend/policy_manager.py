"""PolicyManager — per-pair state machine for user-pinned forwarding paths.

UNSPECIFIED ──[POST /policy]──► POLICY_ACTIVE
     ▲                               │
     │                      [link on path fails]
     │                               │
[DELETE /policy]                     ▼
     └─────────────────────── POLICY_BROKEN
                                    │
                           [POST /policy new path]
                                    │
                                    ▼
                              POLICY_ACTIVE
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

from topology import LinkKey

if TYPE_CHECKING:
    from flow_installer import FlowInstaller
    from host_tracker import HostTracker
    from route_tracker import RouteTracker

LOG = logging.getLogger(__name__)


class PolicyState(Enum):
    """Per-pair policy lifecycle state.

    UNSPECIFIED    — no user-pinned path; default shortest-path routing applies.
    POLICY_ACTIVE  — a user-pinned path is installed and forwarding traffic.
    POLICY_BROKEN  — a link on the pinned path has failed; traffic is stopped.
    """

    UNSPECIFIED = "UNSPECIFIED"
    POLICY_ACTIVE = "POLICY_ACTIVE"
    POLICY_BROKEN = "POLICY_BROKEN"


@dataclass
class PolicyEntry:
    """Stored policy for a single (src_mac, dst_mac) pair.

    ``path`` is a list of ``LinkKey`` edges defining the pinned switch-to-switch
    route.  It is never mutated in-place — replacements create a new
    ``PolicyEntry``.
    """

    state: PolicyState = PolicyState.UNSPECIFIED
    path: list[LinkKey] = field(default_factory=list)


class PolicyManager:
    """Per-pair policy state machine with thread-safe access.

    Stores user-pinned paths.  When ``set_policy()`` is called, the
    old flows (if any) are removed and the new high-priority flows
    are installed via ``FlowInstaller`` (sink→source, symmetric).
    Validates nothing — the caller (RestAPI) is responsible for
    verifying the path is physically traversable in the current graph.
    """

    def __init__(
        self,
        flow_installer: Optional[FlowInstaller] = None,
        host_tracker: Optional[HostTracker] = None,
        route_tracker: Optional[RouteTracker] = None,
    ) -> None:
        self._policies: dict[tuple[str, str], PolicyEntry] = {}
        self._lock = threading.Lock()
        self._flow_installer = flow_installer
        self._host_tracker = host_tracker
        self._route_tracker = route_tracker

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    # ── Read queries ─────────────────────────────────────────────────

    def get_state(self, src_mac: str, dst_mac: str) -> PolicyState:
        """Return the current state for a host pair (thread-safe)."""
        with self._lock:
            entry = self._policies.get((src_mac, dst_mac))
            return entry.state if entry else PolicyState.UNSPECIFIED

    def get_policy(self, src_mac: str, dst_mac: str) -> Optional[PolicyEntry]:
        """Return the full policy entry for the pair, or None."""
        with self._lock:
            return self._policies.get((src_mac, dst_mac))

    def get_policy_path(self, src_mac: str, dst_mac: str) -> Optional[list[LinkKey]]:
        """Return the pinned path (list of LinkKey), or None."""
        entry = self.get_policy(src_mac, dst_mac)
        if entry is not None and entry.state == PolicyState.POLICY_ACTIVE:
            return list(entry.path)
        return None

    def get_all_policies(self) -> dict[str, object]:
        """Return a snapshot for the REST API (raw dict)."""
        result: dict[str, object] = {}
        with self._lock:
            for (src, dst), entry in self._policies.items():
                result[f"{src}→{dst}"] = {
                    "src_mac": src,
                    "dst_mac": dst,
                    "state": entry.state.value,
                    "path": [
                        {
                            "src_dpid": lk.src_dpid,
                            "src_port": lk.src_port,
                            "dst_dpid": lk.dst_dpid,
                            "dst_port": lk.dst_port,
                        }
                        for lk in entry.path
                    ],
                }
        return result

    # ── Mutations ────────────────────────────────────────────────────

    def set_policy(
        self, src_mac: str, dst_mac: str, path: list[LinkKey]
    ) -> PolicyEntry:
        """Store and install a custom forwarding path for a host pair.

        Removes old flows (if any) before installing the new ones.
        The caller MUST have validated the path against the current graph.
        """
        pair = (src_mac, dst_mac)
        with self._lock:
            old = self._policies.get(pair)
            old_path = (
                list(old.path)
                if old is not None and old.state == PolicyState.POLICY_ACTIVE
                else None
            )
            entry = PolicyEntry(state=PolicyState.POLICY_ACTIVE, path=list(path))
            self._policies[pair] = entry

        # Remove old flows outside the lock (I/O — may acquire other locks)
        if old_path:
            self._remove_flows(src_mac, dst_mac, old_path)

        # Convert LinkKey path → dpid list for FlowInstaller
        dpids = _path_to_dpids(path)

        LOG.info(
            "PolicyManager: set_policy %s → %s | dpids=%s | links=%d",
            src_mac,
            dst_mac,
            "→".join(hex(d) for d in dpids),
            len(path),
        )

        # Install forward path (sink→source, high-priority, no timeout)
        fi = self._flow_installer
        if fi is not None:
            forward_links = fi.install_path(dpids, src_mac, dst_mac, is_policy=True)
            reverse_dpids = list(reversed(dpids))
            reverse_links = fi.install_path(
                reverse_dpids, dst_mac, src_mac, is_policy=True
            )
            # Track both directions for fault recovery
            rt = self._route_tracker
            if rt is not None:
                rt.add_route(src_mac, dst_mac, forward_links)
                rt.add_route(dst_mac, src_mac, reverse_links)
            LOG.info(
                "PolicyManager: installed fwd=%d rev=%d links for %s ↔ %s",
                len(forward_links),
                len(reverse_links),
                src_mac,
                dst_mac,
            )

        return entry

    @property
    def all_entries(self) -> dict[tuple[str, str], PolicyEntry]:
        """Return a snapshot of all stored policy entries (thread-safe)."""
        with self._lock:
            return dict(self._policies)

    def remove_policy(self, src_mac: str, dst_mac: str) -> bool:
        """Remove the pinned policy and its flows. Returns True if a policy existed."""
        pair = (src_mac, dst_mac)
        with self._lock:
            entry = self._policies.pop(pair, None)
            if entry is None:
                return False
            removed_path = list(entry.path)

        self._remove_flows(src_mac, dst_mac, removed_path)

        LOG.info("PolicyManager: removed policy for %s → %s", src_mac, dst_mac)
        return True

    def mark_broken(self, src_mac: str, dst_mac: str) -> None:
        """Transition a POLICY_ACTIVE pair to POLICY_BROKEN (link failure)."""
        with self._lock:
            entry = self._policies.get((src_mac, dst_mac))
            if entry is not None and entry.state == PolicyState.POLICY_ACTIVE:
                entry.state = PolicyState.POLICY_BROKEN
                LOG.warning(
                    "PolicyManager: %s → %s → POLICY_BROKEN (link failure)",
                    src_mac,
                    dst_mac,
                )

    def mark_all_affected_broken(self, link: LinkKey) -> list[tuple[str, str]]:
        """Mark all policy pairs traversing *link* as BROKEN.

        Returns the list of affected (src_mac, dst_mac) pairs.
        """
        affected: list[tuple[str, str]] = []
        undirected = link.undirected_key
        with self._lock:
            for pair, entry in self._policies.items():
                if entry.state != PolicyState.POLICY_ACTIVE:
                    continue
                for lk in entry.path:
                    if lk.undirected_key == undirected:
                        entry.state = PolicyState.POLICY_BROKEN
                        affected.append(pair)
                        LOG.warning(
                            "PolicyManager: %s → %s → BROKEN (link %s:%d→%s:%d)",
                            pair[0],
                            pair[1],
                            hex(link.src_dpid),
                            link.src_port,
                            hex(link.dst_dpid),
                            link.dst_port,
                        )
                        break  # one failed link is enough
        return affected

    def delete_all(self) -> None:
        """Remove all policies and their flows (for testing teardown)."""
        with self._lock:
            entries = list(self._policies.items())
            self._policies.clear()
        for pair, entry in entries:
            self._remove_flows(pair[0], pair[1], entry.path)
        LOG.info("PolicyManager: deleted all %d policies", len(entries))

    # ── Internal ─────────────────────────────────────────────────────

    def _remove_flows(self, src_mac: str, dst_mac: str, path: list[LinkKey]) -> None:
        """Remove flow entries on all switches along *path* for both MACs."""
        fi = self._flow_installer
        rt = self._route_tracker
        if fi is not None:
            dpids_to_clean = _collect_dpids(path)
            for dpid in dpids_to_clean:
                fi.delete_flows_for_mac(dpid, src_mac)
                fi.delete_flows_for_mac(dpid, dst_mac)
        if rt is not None:
            rt.remove_route(src_mac, dst_mac)
            rt.remove_route(dst_mac, src_mac)


def _path_to_dpids(path: list[LinkKey]) -> list[int]:
    """Convert a list of LinkKey edges to a list of dpids in traversal order.

    Example: [LinkKey(s1,2, s2,1), LinkKey(s2,3, s3,2)] → [s1, s2, s3]
    """
    if not path:
        return []
    dpids = [path[0].src_dpid]
    for lk in path:
        dpids.append(lk.dst_dpid)
    return dpids


def _collect_dpids(path: list[LinkKey]) -> set[int]:
    """Return all unique dpids referenced in a path."""
    dpids: set[int] = set()
    for lk in path:
        dpids.add(lk.src_dpid)
        dpids.add(lk.dst_dpid)
    return dpids
