# SDN Controller — Lab Experience 2025/26

SDN controller for a LAN (up to 4 hosts, arbitrary topology including rings) built
with **[os-ken](https://github.com/osrg/os-ken)** (OpenFlow 1.3) and **NetworkX**.
Two forwarding planes: **default** (shortest-path with link-failure recovery) and
**policy** (user-pinned path, no recovery). A **FastAPI + uvicorn** REST API exposes
topology, path, port-stats, and policy management endpoints.

## Requirements

- **Linux** (Mininet + Open vSwitch require kernel support)
- Python 3.14.4+
- os-ken 4.1.1+
- FastAPI 0.136+ / uvicorn
- NetworkX 3.6.1+
- Mininet 2.3.0+ & Open vSwitch (for testing)

Install backend dependencies:

```bash
cd src/controller/backend
uv sync
```

Install network dependencies (Mininet tests):

```bash
cd src/controller/network
uv sync
```

## Launching the controller

```bash
cd src/controller/backend
python run.py
```

The `run.py` launcher is **mandatory** — it calls `eventlet.monkey_patch()` before
importing any standard-library module, which is required by os-ken's eventlet hub.

On startup the controller:
1. Wires all internal modules (graph, topology, forwarding, fault handler, etc.)
2. Opens an OpenFlow 1.3 listener on port 6653
3. Starts the **StatsCollector** greenthread (periodic `OFPPortStatsRequest`)
4. Starts the **REST API** on `http://0.0.0.0:8080`

## Architecture

```
Backend (os-ken entry point — event dispatch only)
├── TopologyGraph      — pure NetworkX graph model (no os-ken deps)
├── TopologyManager    — LLDP-based link discovery → graph mutations
├── SpanningTreeManager— BFS spanning tree for loop-free broadcast flooding
├── HostTracker         — mac → (dpid, port) learned from packet-in
├── PathComputer        — symmetric shortest-path (cache + symmetry enforced)
├── RouteTracker        — link → [(src_mac, dst_mac)] for fault recovery
├── ForwardingPlane     — path decision (policy > default) + flow install
│   ├── FlowInstaller   — single OpenFlow write point (sink→source install)
│   └── PolicyManager   — per-pair state machine (UNSPECIFIED/ACTIVE/BROKEN)
├── FaultHandler        — link-failure response (graph update, flow purge, ST)
├── StatsCollector      — periodic OFPPortStatsRequest → shared dict
└── RestAPI             — FastAPI + uvicorn in dedicated thread
```

**Architectural invariants:**
- FlowInstaller is the **only** module that issues `flow_mod` / `packet_out`.
- TopologyGraph is pure Python, zero os-ken dependencies.
- Backend contains zero business logic — it only receives events and delegates.
- RestAPI validates incoming policy paths against the live graph before touching
  PolicyManager.
- PathComputer enforces **symmetric routing**: computing A→B always caches B→A
  with identical links.
- Flows are installed **sink-to-source** to minimise the inconsistency window.
- Policy flows have **priority 20, no idle timeout**; default flows have
  **priority 10, 30 s idle timeout**. Flood rules use priority 1.
- FastAPI runs in a dedicated `threading.Thread` (not an eventlet greenthread)
  to isolate its asyncio event loop from os-ken's eventlet loop.

## Per-pair policy state machine

```
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
```

## Event handling

### Switch connects (`EventOFPSwitchFeatures`, CONFIG_DISPATCHER)

1. Registers the datapath in FlowInstaller.
2. Adds the switch node to TopologyGraph.
3. Attempts port registration from `dp.ports` (may be empty at CONFIG time;
   lazily retried on first packet-in).
4. Installs table-miss flow (priority 0 → CONTROLLER).

### Switch disconnects (`EventOFPStateChange` → DEAD_DISPATCHER)

1. Guards against stale disconnect events (os-ken reconnects create a new
   Datapath before the old one fires a DEAD event).
2. Unregisters the datapath from FlowInstaller.
3. Purges all routes involving the dead switch from RouteTracker.
4. Deletes orphaned flows on all surviving switches.
5. Marks any active policy pairs traversing the dead switch as **BROKEN**.
6. Purges host entries that were attached to the dead switch.
7. Removes the switch and its links from TopologyGraph.
8. Invalidates PathComputer cache, recomputes ST, reinstalls flood rules.

### Link added (`EventLinkAdd` — from LLDP)

1. Ensures both switches' ports are initialised.
2. Adds the link to TopologyGraph (removes edge-port classification for both
   ports).
3. Cleans any hosts wrongly learned on the now-internal ports (broadcast ARP
   during startup can cause HostTracker to absorb MACs on internal ports).
4. Invalidates PathComputer cache (new shorter paths may exist).
5. Recomputes spanning tree and reinstalls flood rules.

### Link deleted (`EventLinkDelete` — from LLDP timeout)

1. Converts the os-ken event to a LinkKey.
2. Delegates to FaultHandler.handle_link_down().

### Port status (`EventOFPPortStatus`)

| Reason               | Behaviour                                                      |
| -------------------- | -------------------------------------------------------------- |
| `DELETE`             | Delegates to `FaultHandler.handle_port_down()`                 |
| `ADD`                | Registers port in graph, recomputes ST, reinstalls flood rules |
| `MODIFY` (link down) | Delegates to `FaultHandler.handle_port_down()`                 |
| `MODIFY` (link up)   | Re-adds port to graph, recomputes ST, reinstalls flood rules   |

### FaultHandler.handle_port_down()

1. Resolves `(dpid, port)` to a LinkKey (if switch-to-switch) or None (edge).
2. **If edge port**: purges all hosts learned on that port, deletes their flows
   from every switch, cleans RouteTracker.
3. Removes the port from TopologyGraph (tears down any associated link).
4. **If switch-to-switch link**: calls `ForwardingPlane.handle_link_failure()`
   to find and delete affected flows, then marks affected policy pairs BROKEN.
5. Recomputs ST and refreshes flood rules.

### Unicast packet-in (`EventOFPPacketIn`)

1. **Broadcast/multicast** (dst `ff:ff:ff:ff:ff:ff` or multicast bit set):
   flooded on spanning-tree ports only. Source MAC is **not** learned.
2. **Unicast**: learns source host location via HostTracker. If the host moved,
   purges stale flows and RouteTracker entries from the old location.
   - If destination is unknown → floods.
   - If source and destination are on the same switch → installs direct edge
     flows (both directions).
   - If the packet arrived at an intermediate switch (not the source's switch)
     → skips install (existing flows or flooding will deliver).
   - If a **policy path** is active for the pair → installs high-priority
     symmetric flows along the pinned path.
   - Otherwise → computes and installs the shortest-path symmetric flows
     (sink→source order), updates RouteTracker.
   - After installation, the buffered packet is forwarded out the correct port
     via `packet_out`.

### Port stats reply (`EventOFPPortStatsReply`)

Delegated to `StatsCollector.on_stats_reply()`. Results are stored in a
`threading.Lock`-protected dict keyed by `dpid → port_no → PortStats`.
The REST API reads directly from this dict — no on-demand OpenFlow interaction.

## REST API

Base URL: `http://localhost:8080`

---

### `GET /path/{src_mac}/{dst_mac}`

Returns the currently active forwarding path between two hosts.

- Source of truth: PolicyManager (policy plane) → RouteTracker (installed
  default flows) → PathComputer (computed, not yet installed).
- **200** — path found:
  ```json
  {
    "src_mac": "00:00:00:00:00:01",
    "dst_mac": "00:00:00:00:00:02",
    "plane": "default | policy",
    "state": "active | unspecified | POLICY_ACTIVE | POLICY_BROKEN",
    "hops": [
      { "dpid": 1, "in_port": 1, "out_port": 2 },
      { "dpid": 2, "in_port": 1, "out_port": 3 }
    ]
  }
  ```
- **404** — one or both MACs unknown to HostTracker.

---

### `GET /stats/ports`

Returns the latest port counters for all interfaces on all connected switches.
Served from StatsCollector's shared dict (no live OpenFlow request).

- **200**:
  ```json
  {
    "switches": [
      {
        "dpid": 1,
        "ports": [
          {
            "port_no": 1,
            "rx_packets": 1024, "tx_packets": 980,
            "rx_bytes": 131072, "tx_bytes": 125440,
            "rx_dropped": 0, "tx_dropped": 0,
            "rx_errors": 0, "tx_errors": 0,
            "last_updated": 1718000000.0
          }
        ]
      }
    ]
  }
  ```
- **503** — StatsCollector has not yet received any reply (first poll cycle
  not complete).

---

### `GET /flows`

Returns the expected flow entries derived from RouteTracker (default plane) and
PolicyManager (policy plane). Shows what SHOULD be installed, not what is
actually on the switches.

- **200**:
  ```json
  {
    "flows": [
      {
        "dpid": 1,
        "match": { "eth_dst": "00:00:00:00:00:02" },
        "out_port": 2,
        "priority": 10,
        "idle_timeout": 30,
        "plane": "default",
        "src_mac": "00:00:00:00:00:01",
        "dst_mac": "00:00:00:00:00:02"
      }
    ]
  }
  ```

---

### `GET /topology`

Returns the full live graph: switches, inter-switch links, known hosts, and the
current spanning-tree edges.

- **200**:
  ```json
  {
    "switches": [1, 2, 3],
    "links": [
      { "src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1 }
    ],
    "hosts": [
      { "mac": "00:00:00:00:00:01", "ip": "10.0.0.0", "dpid": 1, "port": 1 }
    ],
    "spanning_tree": [
      { "src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1 }
    ]
  }
  ```
  Note: `ip` is a placeholder — the controller is L2-only and does not track
  IP addresses.

---

### `GET /policy/{src_mac}/{dst_mac}`

Returns the policy state for a specific host pair.

- **200**:
  ```json
  {
    "src_mac": "00:00:00:00:00:01",
    "dst_mac": "00:00:00:00:00:02",
    "state": "UNSPECIFIED | POLICY_ACTIVE | POLICY_BROKEN",
    "path": [
      { "src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1 }
    ]
  }
  ```
  `path` is `null` when `state` is `UNSPECIFIED`.
- **404** — one or both MACs unknown to HostTracker.

---

### `POST /policy/{src_mac}/{dst_mac}`

Pins a custom forwarding path for the host pair (and its symmetric reverse).
The path is validated against the current topology graph before being stored.

- **200** — path accepted and installed, state → `POLICY_ACTIVE`:
  ```json
  { "message": "Policy installed for 00:00:00:00:00:01 → 00:00:00:00:00:02" }
  ```
- **400** — path is not physically traversable (specific error message naming
  the invalid link).
- **404** — one or both MACs unknown to HostTracker.
- **409** — src and dst are the same host.

Request body:
```json
{
  "path": [
    { "src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1 },
    { "src_dpid": 2, "src_port": 3, "dst_dpid": 3, "dst_port": 2 }
  ]
}
```

---

### `DELETE /policy/{src_mac}/{dst_mac}`

Removes the pinned policy for the pair. Flows are deleted from all switches.
The next packet-in triggers default shortest-path routing as `UNSPECIFIED`.

- **200** — policy removed:
  ```json
  { "message": "Policy removed for 00:00:00:00:00:01 → 00:00:00:00:00:02" }
  ```
- **404** — no active policy exists for this pair, or MACs unknown.

---

## Testing with Mininet

Test topologies live in `src/controller/network/`:

```bash
cd src/controller/network
sudo uv run python linear.py     # h1—s1—s2—s3—h2
sudo uv run python ring.py       # same + s1—s3 extra link
sudo uv run python full_mesh.py  # 3 switches, fully meshed
```

Each script:
1. Starts a Mininet network with the specified topology.
2. Connects all switches to the controller at `127.0.0.1:6653`.
3. Assigns IPs and MACs, then runs `pingall` to trigger host discovery and
   path installation.

For manual testing, start the controller first, then launch a Mininet topology
in another terminal.

**Expected behaviour after `pingall`:**
- All hosts can ping each other (default shortest-path forwarding).
- `GET /topology` shows all switches, links, and hosts.
- `GET /path/{h1}/{h2}` shows the active path.
- `POST /policy/{h1}/{h2}` with a valid alternative path installs high-priority
  flows. Subsequent pings use the pinned path.
- Bringing down a link on the policy path transitions the pair to
  `POLICY_BROKEN` (`GET /policy/{h1}/{h2}` shows the state change).
- `DELETE /policy/{h1}/{h2}` restores default routing.
- `GET /stats/ports` shows port counters updating every ~5 s.
