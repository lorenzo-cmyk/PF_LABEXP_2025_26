# SDN Controller Backend — Lab Experience 2025/26

SDN controller for a LAN (up to 4 hosts, arbitrary topology including rings)
built with **os-ken** (OpenFlow 1.3) and **NetworkX**.

Two forwarding planes: **default** (shortest-path with link-failure recovery) and
**policy** (user-pinned path, no auto-fallback). A **FastAPI + uvicorn** REST API
exposes topology, path, port-stats, policy management, live event counters, and
a ring-buffered log store.

---

## Requirements

- **Linux** (Mininet + Open vSwitch require kernel support)
- Python 3.14.4+
- os-ken 4.1.1+
- FastAPI 0.136+ / uvicorn
- NetworkX 3.6.1+
- [**uv**](https://docs.astral.sh/uv/) (project manager — replaces pip)

Install dependencies:

```bash
cd src/controller/backend
uv sync
```

---

## Launching the controller

```bash
cd src/controller/backend
uv run python run.py
```

The `run.py` launcher is **mandatory** — it calls `eventlet.monkey_patch()` before
importing any standard-library module, which is required by os-ken's eventlet hub.

On startup the controller:

1. Wires all internal modules (graph, topology, forwarding, fault handler, etc.)
2. Opens an OpenFlow 1.3 listener on **port 6653**
3. Starts the **StatsCollector** greenthread (periodic `OFPPortStatsRequest`, every 5 s)
4. Starts the **REST API** on `http://0.0.0.0:8080`

---

## Architecture

```
Backend (os-ken entry point — event dispatch only)
├── TopologyGraph       — pure NetworkX graph model (no os-ken deps)
├── TopologyManager     — LLDP-based link discovery → graph mutations
├── HostTracker         — mac → (dpid, port) learned from packet-in
├── PathComputer        — symmetric shortest-path (cache + symmetry enforced)
├── RouteTracker        — link → [(src_mac, dst_mac)] for fault recovery
├── ForwardingPlane     — path decision (policy > default) + flow install
│   ├── FlowInstaller   — single OpenFlow write point (sink→source install)
│   └── PolicyManager   — per-pair state machine (UNSPECIFIED/ACTIVE/BROKEN)
├── FaultHandler        — link-failure response (graph update, flow purge)
├── StatsCollector      — periodic OFPPortStatsRequest → shared dict
├── LogStore            — ring-buffer logging.Handler captured to REST API
├── EventCounters       — thread-safe cumulative event counters
└── RestAPI             — FastAPI + uvicorn in dedicated thread
```

### Architectural invariants

- FlowInstaller is the **only** module that issues `flow_mod` / `packet_out`.
- TopologyGraph is pure Python, zero os-ken dependencies.
- Backend contains zero business logic — it only receives events and delegates.
- RestAPI validates incoming policy paths against the live graph before touching
  PolicyManager.
- PathComputer enforces **symmetric routing**: computing A→B always caches B→A
  with identical links.
- Flows are installed **sink-to-source** to minimise the inconsistency window.
- Policy flows have **priority 20, no idle timeout** (sticky); default flows have
  **priority 10, 30 s idle timeout**. Table-miss entries use priority 0.
- FastAPI runs in a dedicated `threading.Thread` (not an eventlet greenthread)
  to isolate its asyncio event loop from os-ken's eventlet loop.

---

## Event handling

### Switch connects (`EventOFPSwitchFeatures`, CONFIG_DISPATCHER)

1. Registers the datapath in FlowInstaller.
2. Adds the switch node to TopologyGraph.
3. Attempts port registration from `dp.ports` (may be empty at CONFIG time;
   lazily retried on first packet-in).
4. Requests switch description via `OFPMP_DESC` to classify vendor (OVS vs
   HPE vs Zodiac FX).
5. On DESC reply (or 2 s timeout fallback):
   - Installs the table-miss flow (priority 0 → CONTROLLER) on the switch's
     primary flow table (table 0 for OVS/Zodiac, table 100 for HPE).
   - Installs permanent high-priority drop rules: IPv6 (ethertype 0x86DD)
     and IPv4 multicast (dst MAC 01:00:5e/9, OVS/Zodiac only — HPE table 100
     rejects masked eth_dst matches). LLDP and ARP are *not* dropped in
     hardware — LLDP is handled by os-ken's built-in Switches app and ARP by
     the controller's proxy-ARP handler.
6. Invalidates path cache.

### Switch disconnects (`EventOFPStateChange` → DEAD_DISPATCHER)

1. Guards against stale disconnect events (os-ken reconnects create a new
   Datapath before the old one fires a DEAD event).
2. Unregisters the datapath from FlowInstaller.
3. Purges all routes involving the dead switch from RouteTracker.
4. Deletes orphaned flows on all surviving switches.
5. Marks any active policy pairs traversing the dead switch as **BROKEN**.
6. Purges host entries that were attached to the dead switch.
7. Removes the switch and its links from TopologyGraph.
8. Invalidates PathComputer cache.

### Link added (`EventLinkAdd` — from LLDP)

1. Ensures both switches' ports are initialised.
2. Adds the link to TopologyGraph (removes edge-port classification for both
   ports).
3. Cleans any hosts wrongly learned on the now-internal ports (broadcast ARP
   during startup can cause HostTracker to absorb MACs on internal ports).
4. Invalidates PathComputer cache (new shorter paths may exist).

### Link deleted (`EventLinkDelete` — from LLDP timeout)

1. Converts the os-ken event to a LinkKey.
2. Delegates to `FaultHandler.handle_link_down()`.

### Port status (`EventOFPPortStatus`)

| Reason               | Action                                                         |
| -------------------- | -------------------------------------------------------------- |
| `DELETE`             | Delegates to `FaultHandler.handle_port_down()`                 |
| `ADD`                | Registers port in graph via `TopologyManager.port_add()`       |
| `MODIFY` (link down) | Delegates to `FaultHandler.handle_port_down()`                 |
| `MODIFY` (link up)   | Re-adds port to graph via `TopologyManager.port_modify()`      |

### `FaultHandler.handle_port_down(dpid, port)`

1. Resolves `(dpid, port)` to a LinkKey (if switch-to-switch) or None (edge).
2. **If edge port** (no link, not internal):
   - Purges all hosts learned on that port.
   - Deletes their flows from every switch.
   - Purges RouteTracker entries involving those hosts.
   - Marks all policies involving those hosts as BROKEN.
3. Removes the port from TopologyGraph (also tears down any associated link).
4. **If switch-to-switch link**: calls `ForwardingPlane.handle_link_failure()`
   to find and delete affected flows, then marks affected policy pairs BROKEN.

### Unicast packet-in (`EventOFPPacketIn`)

1. **LLDP packets** (dst `01:80:c2:00:00:0e`) → delegated to os-ken's built-in
   switches app. Return immediately.
2. **Source host learning**: if the source MAC is unicast and the ingress port
   is not a known internal link port, the host location is learned/updated in
   HostTracker.
3. **ARP processing:**
   - **ARP Request**: source IP is learned. Proxy ARP: if the target IP is
     known, an ARP Reply is crafted and sent back to the requester. If
     unknown, silently dropped. Gratuitous ARP (src_ip == dst_ip) is dropped.
   - **ARP Reply**: both sender and recipient IP→MAC mappings are learned.
4. **Broadcast/multicast** (dst `ff:ff:ff:ff:ff:ff` or multicast bit set):
   silently dropped (zero-trust). Exception: ARP is handled above.
5. **Unicast IPv4**:
   - Source IP is learned from the IPv4 header.
   - If destination is unknown in HostTracker → drop.
   - If the packet arrived at an intermediate switch (not the source's switch)
     → skip install (existing flows or timeout will deliver).
    - If source and destination are on the same switch → installs direct edge
      flows (both directions, priority 10, 30 s idle timeout) and tracks the
      pair in RouteTracker (so `GET /flows` includes them).
   - If a **policy path** is ACTIVE for the pair → installs high-priority
     symmetric flows along the pinned path (priority 20, no idle timeout).
   - If a policy exists but is **BROKEN** → drop (no auto-fallback).
   - Otherwise → computes and installs the shortest-path symmetric flows
     (sink→source order, priority 10, 30 s idle timeout), updates RouteTracker.
   - After installation, the buffered packet is forwarded out the correct port
     via `packet_out`.

### Host discovery events (os-ken built-in)

- **`EventHostAdd`**: records `(mac, dpid, port)` in HostTracker.
- **`EventHostMove`**: updates HostTracker location, purges stale RouteTracker
  entries, deletes orphaned flows on all switches, marks policies involving the
  moved MAC as BROKEN, invalidates path cache.

### Port stats reply (`EventOFPPortStatsReply`)

Delegated to `StatsCollector.on_stats_reply()`. Results are stored in a
`threading.Lock`-protected dict keyed by `dpid → port_no → PortStats`.
The REST API reads directly from this dict — no on-demand OpenFlow interaction.

---

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

- **UNSPECIFIED**: no user-pinned path; default shortest-path routing applies.
- **POLICY_ACTIVE**: a user-pinned path is installed. High-priority flows
  (priority 20) with no idle timeout. Traffic on this pair always uses the
  pinned path.
- **POLICY_BROKEN**: a link on the pinned path has failed. Sticky flows are
  removed. Traffic is dropped — no automatic fallback to default routing.

---

## REST API

Base URL: `http://localhost:8080`

**OpenAPI/Swagger UI is available** at `http://localhost:8080/docs` (FastAPI
auto-generates it).

All endpoints return JSON. FastAPI/uvicorn runs in a dedicated thread (not
an eventlet greenthread) to isolate its asyncio event loop from os-ken.

---

### `GET /path/{src_mac}/{dst_mac}`

Returns the currently active forwarding path between two hosts.

- Source of truth: PolicyManager (policy plane) → RouteTracker (installed
  default flows) → PathComputer (computed, not yet installed).
- Checks both directions (forward and reverse policy entries).

**200** — path found:

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

**200**:

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

- **503** — StatsCollector has not yet received any reply.

---

### `GET /flows`

Returns the expected flow entries derived from RouteTracker (default plane) and
PolicyManager (policy plane). Shows what SHOULD be installed — not a live
dump from the switches.

**200**:

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

Returns the full live graph: switches (with vendor metadata), inter-switch links,
and known hosts.

**200**:

```json
{
  "switches": [
    {
      "dpid": 1,
      "vendor": "Open vSwitch",
      "hw_desc": "Open vSwitch",
      "sw_desc": "2.17.0",
      "num_ports": 4,
      "main_table": 0
    }
  ],
  "links": [
    { "src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1 }
  ],
  "hosts": [
    { "mac": "00:00:00:00:00:01", "ips": ["10.0.0.1"], "dpid": 1, "port": 1 }
  ]
}
```

---

### `GET /policy/{src_mac}/{dst_mac}`

Returns the policy state for a specific host pair.

**200**:

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
Old sticky flows (if any) are deleted, new flows installed at priority 20 with
no idle timeout.

**200** — path accepted and installed, state → `POLICY_ACTIVE`:

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

Validations performed by the API before reaching PolicyManager:

- Path is non-empty.
- First link starts from the source host's switch.
- Last link ends at the destination host's switch.
- Links form a contiguous chain (link[k].dst == link[k+1].src).
- Each link exists in the current topology graph.
- Port numbers match those in the live graph.

---

### `DELETE /policy/{src_mac}/{dst_mac}`

Removes the pinned policy for the pair. Sticky flows are deleted from all
switches. The next packet-in will trigger default shortest-path routing
(`UNSPECIFIED` state).

**200** — policy removed:

```json
{ "message": "Policy removed for 00:00:00:00:00:01 → 00:00:00:00:00:02" }
```

- **404** — no active policy exists for this pair, or MACs unknown.

---

### `GET /logs`

Returns ring-buffered log entries captured since the controller started. All
entries include an ISO‑8601 local-time timestamp, log level, logger name, and
the raw message text. The existing stdout logging is unaffected.

**Query parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `level`   | `DEBUG` | Minimum severity (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). Entries below this threshold are excluded. |
| `lines`   | `ALL`   | Number of most-recent entries to return, or `ALL` for every matching entry. |

**200**:

```json
{
  "level": "DEBUG",
  "lines": "ALL",
  "returned": 141,
  "entries": [
    {
      "timestamp": "2026-04-30T09:52:45.617+02:00",
      "level": "INFO",
      "logger": "stats_collector",
      "message": "StatsCollector: created (poll_interval=5.0s)"
    }
  ]
}
```

---

### `GET /events`

Returns cumulative counters for all network events since controller startup.
Values are monotonic and never reset.

**200**:

```json
{
  "switch_connected": 3,
  "switch_disconnected": 0,
  "link_up": 6,
  "link_down": 0,
  "host_added": 3,
  "host_moved": 0,
  "port_up": 0,
  "port_down": 0,
  "packets_forwarded": 3,
  "packets_dropped": 0,
  "arp_replies_sent": 6,
  "policies_installed": 0,
  "policies_removed": 0
}
```

| Counter | Description |
|---------|-------------|
| `switch_connected` | Total switch connection events |
| `switch_disconnected` | Total switch disconnection events |
| `link_up` | Link discovered (LLDP) |
| `link_down` | Link deleted (LLDP timeout or port down) |
| `host_added` | MAC address newly discovered |
| `host_moved` | MAC address moved to a different port |
| `port_up` | Port added or link state became UP |
| `port_down` | Port deleted or link state became DOWN |
| `packets_forwarded` | Unicast packets forwarded via `packet_out` |
| `packets_dropped` | Packets silently dropped (unknown dst, BROKEN policy, no path, broadcast/multicast) |
| `arp_replies_sent` | Proxy ARP replies crafted and sent |
| `policies_installed` | User-pinned paths installed via `POST /policy` |
| `policies_removed` | User-pinned paths removed via `DELETE /policy` |

Counters are incremented by the module where the event naturally occurs:

| Source | Counters |
|--------|----------|
| `HostTracker.add_host()` | `host_added`, `host_moved` |
| `TopologyManager.port_add/delete/modify` | `port_up`, `port_down` |
| `TopologyManager.link_add/delete` | `link_up`, `link_down` |
| `Backend` packet-in handler | `switch_connected`, `switch_disconnected`, `packets_forwarded`, `packets_dropped`, `arp_replies_sent` |
| `RestAPI` POST/DELETE policy | `policies_installed`, `policies_removed` |

---

Test topologies and network configuration live in `src/controller/network/`.
See its README for details.
