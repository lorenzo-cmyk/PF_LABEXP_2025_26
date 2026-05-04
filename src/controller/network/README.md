# SDN Controller Network — Lab Experience 2025/26

Mininet topologies and integration tests for the backend SDN controller.
Uses **Mininet** with Open vSwitch, connecting switches to a remote
os-ken controller at `127.0.0.1:6653`.

---

## Requirements

- **Linux** (Mininet + Open vSwitch require kernel support)
- Python 3.14.4+
- Mininet 2.3.0+
- [**uv**](https://docs.astral.sh/uv/) (project manager)

Install dependencies:

```bash
cd src/controller/network
uv sync
```

---

## Topology spawners (interactive Mininet console)

These scripts start a Mininet network and drop you into a CLI prompt.
Useful for manual exploration or debugging. Press `Ctrl+D` to exit.

| Script | Topology | Description |
|--------|----------|-------------|
| `linear.py` | `h1—s1—s2—s3—h2` | 3 switches in a line, 2 hosts |
| `ring.py` | linear + `s1—s3` | 3-switch ring, 2 hosts |
| `full_mesh.py` | 3 switches fully meshed | Each switch connected to every other switch, 3 hosts |

**How to run** (start the controller first, then the topology):

```bash
# Terminal 1 — start the controller
cd src/controller/backend
uv run python run.py

# Terminal 2 — start a topology
cd src/controller/network
sudo uv run python linear.py
# or: sudo uv run python ring.py
# or: sudo uv run python full_mesh.py
```

---

## Tests

All `test_*.py` files are automated integration tests. Each test:

1. Starts its own Mininet topology.
2. Connects switches to `127.0.0.1:6653`.
3. Runs a series of ping/API checks.
4. Prints **PASS** or **FAIL** and exits with code 0/1.

Each test is **standalone** — it expects the controller to already be
running on port 6653.

### Running a single test

```bash
# Terminal 1 — start the controller
cd src/controller/backend
uv run python run.py

# Terminal 2 — run one test
cd src/controller/network
sudo uv run python test_link_failure.py
```

### Running all tests

The `test_runner.sh` script manages the controller lifecycle automatically:
it starts the controller, runs each test, stops the controller, and repeats.

```bash
cd src/controller/network
sudo ./test_runner.sh
```

Controller and test output are saved to `results/run_YYYYMMDD_HHMMSS/`.
The script prints a pass/fail summary when done.

**Run a subset of tests** matching a substring:

```bash
sudo ./test_runner.sh rest_api        # all REST API tests
sudo ./test_runner.sh link_failure    # just test_link_failure.py
sudo ./test_runner.sh edge            # all edge case tests
```

---

### Functional tests

| Test | What it verifies |
|------|-----------------|
| `test_link_failure.py` | Single link failure: traffic reroutes through alternate path in a ring, then recovers when the link is restored. |
| `test_multi_failure.py` | Progressive multiple link failures in a full mesh — verifies correct isolation when a host is cut off. |
| `test_partition.py` | Network partition into two islands: intra-island traffic keeps working, cross-island traffic fails gracefully, full recovery on bridge restoration. |
| `test_iperf_stress.py` | Stress test with live iperf traffic: policy install/uninstall, link flap, switch disconnect/reconnect — verifies REST API and data plane under load. |

### REST API tests

| Test | Endpoints tested |
|------|-----------------|
| `test_rest_api_topology.py` | `GET /topology` — switches, links, hosts, no phantom entries. `GET /path` — path exists, hop structure valid, 404 for unknown MACs. |
| `test_rest_api_policy.py` | Full policy CRUD lifecycle: `POST /policy`, `GET /policy`, `DELETE /policy`. Invalid path (wrong port, non-contiguous, empty, missing fields, unparseable dpids), same MAC → 409, unknown MAC → 404. |
| `test_rest_api_flows.py` | `GET /flows` — default flows for pairs, policy flows at priority 20, policy flows remove default flows, flow entries carry correct source/pair metadata. |
| `test_rest_api_stats.py` | `GET /stats/ports` — 503 when no data yet, counters increase, per-port structure, `last_updated` timestamps. |
| `test_rest_api_failure.py` | REST API behavior under failure: topology updates after link down, path state in RouteTracker, policy state transitions to BROKEN. |
| `test_rest_api_logs_events.py` | `GET /logs` — default params, level filtering, line limit, combined filters. `GET /events` — all event keys present, non-zero after startup and traffic. |

### Edge case tests

| Test | What it verifies |
|------|-----------------|
| `test_edge_equal_cost.py` | Equal-cost tie-breaking — traffic survives repeated path transitions between two equal-cost paths in a diamond topology during constant failure. |
| `test_edge_flow_table_consistency.py` | Controller view (`GET /flows`) vs. actual OVS flow tables (`ovs-ofctl dump-flows`) — catches phantom flows and hardware desync. |
| `test_edge_ghost_flows.py` | No stale flow accumulation after 6 repeated fail-recover cycles. |
| `test_edge_graph_restore.py` | The "better path returns" scenario: when a link comes back UP after being DOWN, the graph and path cache must immediately use it if the backup path subsequently fails. |
| `test_edge_host_relocate.py` | Host mobility: a device unplugs from one switch and plugs into another (same MAC, IP moved). Edge-port-down purge must clean stale flows. |
| `test_edge_idle_timeout.py` | Flow expiry after 30s idle — the controller must fully re-install flows (no OpenFlow notification for idle timeout). |
| `test_edge_k4_mesh.py` | K4 full-mesh (4 switches, 6 links) — proxy-ARP and zero-trust broadcast drop prevent storms in a heavily connected topology. |
| `test_edge_link_restore_cache.py` | Stale path cache after link recovery — no manual invalidation on link up. A previously-cached suboptimal path must not persist after a better path becomes available. |
| `test_edge_mobility_roundtrip.py` | Round-trip host mobility: a host moves from s1→s3, then back s3→s1. The second purge must clean stale flows on the second switch. |
| `test_edge_policy_bidirectional.py` | Policy installs flows in both directions. Reverse direction mirrors the forward path. Link failure cleanup removes flows in both directions. |
| `test_edge_policy_link_recovery.py` | Pinned path does NOT auto-recover when a failed link comes back — stays BROKEN until admin re-pins or deletes. |
| `test_edge_port_up_arp.py` | Host re-discovery after port-up: when an edge port goes down then up, the host must re-announce itself via its own outbound packet (no ARP flooding). |
| `test_edge_rapid_flap.py` | Rapid link flapping (5 up/down cycles in quick succession) — controller stays stable, no state corruption. |
| `test_edge_reconnect_race.py` | Switch disconnect/reconnect race: 5 rapid disconnect/reconnect cycles. The controller must guard against stale DEAD events from old Datapath instances. |
| `test_edge_same_switch.py` | Same-switch fast path: when src_dpid == dst_dpid, a direct flow is installed without path computation. Same-switch flows survive inter-switch link failures. |
| `test_edge_selective_reroute.py` | RouteTracker isolation: when a link fails, only flows traversing that link are purged. Other pairs traversing other links are unaffected. |
| `test_edge_simultaneous.py` | Concurrency: multiple links fail simultaneously. The controller handles concurrent port-down events without race conditions. |
| `test_edge_st_irrelevant.py` | Fault isolation: a fault on a link not used by an active flow does not disrupt existing connectivity. |
| `test_edge_stale_host.py` | Startup race: packets arrive before LLDP discovers inter-switch links. Hosts wrongly learned on internal ports are cleaned when LLDP confirms the link. |
| `test_edge_switch_rebirth.py` | Full switch lifecycle: OFP channel drops → DEAD → controller purges routes/flows/hosts → switch reconnects → full recovery. |

---

## Cleanup

If a test leaves stale Mininet state behind:

```bash
sudo mn -c
```

This removes all lingering namespaces, bridges, and interfaces.
