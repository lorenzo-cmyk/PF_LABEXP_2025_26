"""Test for Bug 2: Broken sink-flow deduplication in GET /flows.

The sink-flow dedup check in ``get_flows()`` uses a slice of the *global*
entries list (``entries[-len(links):]``) rather than a per-route window.
When two or more routes share the same sink switch, the sink entry for
the later-processed route can be silently omitted from the API response
even though the flow exists in hardware.

Combined with Bug 1 (reversed LinkKey order causing ``sink_dpid`` to
point to the wrong switch), sink flows are *systematically* missing
for every multi-hop route in the default plane.

This test uses a topology where two hosts on different edge switches
both send to a third host on a shared sink switch:
   h1—s1—s2—s3—h3
   h2—s4—s5—s3
After pings trigger both routes, it checks:
  1. Each route has a flow entry on the sink switch (dpid=3) pointing
     toward the destination host's edge port.
  2. All flow entries on the sink switch reference known dst MACs.
"""

import sys
import time
import json
import subprocess
import urllib.request
import urllib.error

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info

API_BASE = "http://127.0.0.1:8080"
H1_MAC = "00:00:00:00:00:01"
H2_MAC = "00:00:00:00:00:02"
H3_MAC = "00:00:00:00:00:03"


def _api_get(path: str) -> tuple[int, dict | None]:
    url = f"{API_BASE}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError, ValueError:
            return e.code, {"detail": body}
    except urllib.error.URLError:
        return -1, None


def test_sink_dedup():
    """Verify sink-switch flows are not silently omitted from GET /flows."""
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")
    s4 = net.addSwitch("s4", dpid="0000000000000004")
    s5 = net.addSwitch("s5", dpid="0000000000000005")

    h1 = net.addHost("h1", ip="10.0.0.1", mac=H1_MAC)
    h2 = net.addHost("h2", ip="10.0.0.2", mac=H2_MAC)
    h3 = net.addHost("h3", ip="10.0.0.3", mac=H3_MAC)

    # Route A: h1  — s1 — s2 — s3 — h3
    net.addLink(h1, s1)
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    # Route B: h2  — s4 — s5 — s3 — h3  (shares sink s3)
    net.addLink(h2, s4)
    net.addLink(s4, s5)
    net.addLink(s5, s3)
    # h3 on sink switch s3
    net.addLink(h3, s3)

    net.build()
    net.start()

    info("*** Pinging to learn hosts\n")
    net.pingAll()

    info("*** Waiting for topology discovery\n")
    time.sleep(6)

    info("*** Pinging to install flows for both routes\n")
    net.pingAll()
    time.sleep(2)

    passed = True

    # ── 1. Verify h3 lives on s3 ──────────────────────────────────────
    info("*** 1. Checking h3 is on switch s3 (dpid=3)\n")
    status, topo = _api_get("/topology")
    if status != 200 or not topo or "hosts" not in topo:
        info("FAIL: /topology unavailable\n")
        passed = False
    else:
        h3_hosts = [h for h in topo["hosts"] if h["mac"] == H3_MAC]
        if not h3_hosts:
            info(f"FAIL: h3 ({H3_MAC}) not found in /topology\n")
            passed = False
        elif h3_hosts[0]["dpid"] != 3:
            info(f"FAIL: h3 expected on dpid=3, got dpid={h3_hosts[0]['dpid']}\n")
            passed = False
        else:
            info(f"PASS: h3 is on dpid=3 port={h3_hosts[0]['port']}\n")

    # ── 2. Both routes should have a flow on sink switch s3 ───────────
    info("*** 2. Checking sink switch s3 has flow entries for both routes\n")
    status, data = _api_get("/flows")
    if status != 200 or not data or "flows" not in data:
        info("FAIL: /flows unavailable\n")
        passed = False
    else:
        flows = data["flows"]
        keyed_by_src = {}
        for f in flows:
            (f.get("src_mac"), f.get("dst_mac"))
            if f["dpid"] == 3 and f.get("plane") == "default":
                keyed_by_src.setdefault(f.get("src_mac"), []).append(f)

        for src_mac, label in [(H1_MAC, "h1"), (H2_MAC, "h2")]:
            sink_flows = keyed_by_src.get(src_mac, [])
            if not sink_flows:
                info(
                    f"FAIL: no default-plane flow on sink s3 for "
                    f"{label}→h3 — sink flow is missing\n"
                )
                passed = False
            else:
                out_ports = {f["out_port"] for f in sink_flows}
                if all(p > 0 for p in out_ports):
                    info(
                        f"PASS: sink s3 has flow for {label}→h3 "
                        f"(out_ports={out_ports})\n"
                    )
                else:
                    info(
                        f"FAIL: sink flow for {label}→h3 has "
                        f"zero/negative out_port: {out_ports}\n"
                    )
                    passed = False

    # ── 3. All flows on sink s3 should reference valid MACs ───────────
    info("*** 3. All sink-switch flows reference known hosts\n")
    if status == 200 and data and data.get("flows"):
        known_macs = {H1_MAC, H2_MAC, H3_MAC}
        sink_all = [f for f in data["flows"] if f["dpid"] == 3]
        for f in sink_all:
            if f.get("src_mac") not in known_macs or f.get("dst_mac") not in known_macs:
                info(
                    f"FAIL: sink flow references unknown MAC: "
                    f"src={f.get('src_mac')} dst={f.get('dst_mac')}\n"
                )
                passed = False
                break
        else:
            info("PASS: all sink-switch flows reference known MACs\n")

    # ── 4. Each route path has flow entries on ALL traversed switches ─
    info("*** 4. Route A (h1→h3 via s1-s2-s3) covers all 3 switches\n")
    route_a_flows = [
        f
        for f in data.get("flows", [])
        if f.get("plane") == "default"
        and f.get("src_mac") == H1_MAC
        and f.get("dst_mac") == H3_MAC
    ]
    route_a_dpids = sorted(set(f["dpid"] for f in route_a_flows))
    info(f"  Route A dpids: {route_a_dpids}\n")
    if route_a_dpids == [1, 2, 3]:
        info("PASS: route A covers switches [1, 2, 3]\n")
    elif route_a_dpids == [1, 2]:
        info(
            "FAIL: route A missing sink switch dpid=3 "
            "(expected [1,2,3], got [1,2]) — Bug 2 sink dedup\n"
        )
        passed = False
    else:
        info(f"FAIL: route A unexpected dpids {route_a_dpids} (expected [1,2,3])\n")
        passed = False

    info("*** 5. Route B (h2→h3 via s4-s5-s3) covers all 3 switches\n")
    route_b_flows = [
        f
        for f in data.get("flows", [])
        if f.get("plane") == "default"
        and f.get("src_mac") == H2_MAC
        and f.get("dst_mac") == H3_MAC
    ]
    route_b_dpids = sorted(set(f["dpid"] for f in route_b_flows))
    info(f"  Route B dpids: {route_b_dpids}\n")
    if route_b_dpids == [3, 4, 5]:
        info("PASS: route B covers switches [4, 5, 3]\n")
    elif route_b_dpids == [4, 5]:
        info(
            "FAIL: route B missing sink switch dpid=3 "
            "(expected [3,4,5], got [4,5]) — Bug 2 sink dedup\n"
        )
        passed = False
    else:
        info(f"FAIL: route B unexpected dpids {route_b_dpids} (expected [3,4,5])\n")
        passed = False

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print("\033[91m                 FAIL                    \033[0m")
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Bug 2: Sink-Flow Deduplication Test ---\n")
    test_sink_dedup()
