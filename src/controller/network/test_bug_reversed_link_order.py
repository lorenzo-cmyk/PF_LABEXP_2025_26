"""Test for Bug 1: Reversed LinkKey order in FlowInstaller.install_path.

install_path walks the path backwards (sink→source) but stores LinkKeys
in that same reversed order in RouteTracker.  Consumers that assume
source→sink order (e.g. GET /path, GET /flows) then produce garbled
hop sequences with duplicate dpids and incorrect sink-switch flows.

This test uses a linear 3‑switch topology with NO backup link so the
only path between h1 and h2 is s1→s2→s3.  After pings trigger flow
installation, it checks:
  1. GET /path dpids are [1, 2, 3] — no duplicates, correct order.
  2. The sink switch (s3) has a flow entry for the destination MAC.
  3. Total unique dpids in the route match the hop count.
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


def test_reversed_link_order():
    """Verify RouteTracker LinkKeys are stored in source→sink order."""
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    h1 = net.addHost("h1", ip="10.0.0.1", mac=H1_MAC)
    h2 = net.addHost("h2", ip="10.0.0.2", mac=H2_MAC)

    # Strictly linear — only one path between h1 and h2.
    net.addLink(h1, s1)
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s3, h2)

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for topology discovery\n")
    time.sleep(6)

    info("*** Pinging to trigger flow installation on s1→s2→s3\n")
    net.pingAll()
    time.sleep(2)

    passed = True

    # ── 1. GET /path — dpids must be [1,2,3] in order, no duplicates ──
    info("*** 1. GET /path — checking dpids are sequential in source→sink order\n")
    status, data = _api_get(f"/path/{H1_MAC}/{H2_MAC}")
    if status != 200:
        info(f"FAIL: /path returned {status}\n")
        passed = False
    elif not data or "hops" not in data:
        info("FAIL: /path missing 'hops' key\n")
        passed = False
    elif not data["hops"]:
        info("FAIL: /path returned empty hops\n")
        passed = False
    else:
        dpids = [hop["dpid"] for hop in data["hops"]]
        expected = [1, 2, 3]
        info(f"  Hops dpids: {dpids}\n")
        if dpids == expected:
            info("PASS: /path dpids are [1, 2, 3] — correct source→sink order\n")
        elif len(set(dpids)) != len(dpids):
            info(
                f"FAIL: dpids contain duplicates {dpids} "
                f"(expected {expected}, likely reversed LinkKey order)\n"
            )
            passed = False
        elif dpids != sorted(dpids):
            info(
                f"FAIL: dpids are out of order {dpids} "
                f"(expected {expected}, likely reversed LinkKey order)\n"
            )
            passed = False
        else:
            info(f"FAIL: unexpected dpids {dpids} (expected {expected})\n")
            passed = False

    # ── 2. GET /path — verify no dpid appears more than once ───────────
    info("*** 2. GET /path — verifying no duplicate dpids in hop chain\n")
    if status == 200 and data and data.get("hops"):
        dpids = [hop["dpid"] for hop in data["hops"]]
        if len(dpids) != len(set(dpids)):
            info(
                f"FAIL: duplicate dpids in hop chain: {dpids} "
                f"(this indicates reversed LinkKey order)\n"
            )
            passed = False
        elif len(dpids) != 3:
            info(
                f"FAIL: expected exactly 3 dpids in hop chain, got {len(dpids)}: {dpids}\n"
            )
            passed = False
        else:
            info(
                f"PASS: no duplicate dpids — hop chain has {len(dpids)} unique switches\n"
            )

    # ── 3. GET /flows — sink switch (s3, dpid=3) must have a flow ─────
    info("*** 3. GET /flows — sink switch (s3) must have flow for dst MAC\n")
    status, data = _api_get("/flows")
    if status != 200 or not data or "flows" not in data:
        info("FAIL: /flows unavailable\n")
        passed = False
    else:
        flows = data["flows"]
        # Find default-plane flows on the destination switch (s3) for h2's MAC
        sink_flows = [
            f
            for f in flows
            if f.get("dpid") == 3
            and f.get("dst_mac") == H2_MAC
            and f.get("plane") == "default"
        ]
        if not sink_flows:
            info(
                "FAIL: no default-plane flow on sink switch s3 for dst_mac "
                f"{H2_MAC} — sink flow is missing (reversed LinkKey bug)\n"
            )
            passed = False
        else:
            for sf in sink_flows:
                info(
                    f"  sink flow: dpid={sf['dpid']} out_port={sf['out_port']} "
                    f"src={sf.get('src_mac')} dst={sf['dst_mac']}\n"
                )
            # Verify the out_port is an actual host-facing port (positive)
            all_valid = all(f.get("out_port", -1) > 0 for f in sink_flows)
            if all_valid:
                info("PASS: sink switch s3 has valid flow entry for dst_mac\n")
            else:
                info("FAIL: sink flow has invalid out_port\n")
                passed = False

    # ── 4. Extra: all default-plane flows for h1↔h2 span the right dpids
    info("*** 4. Checking all route dpids are known switches [1,2,3]\n")
    if status == 200 and data and data.get("flows"):
        flows = data["flows"]
        route_flows = [
            f
            for f in flows
            if f.get("plane") == "default"
            and f.get("src_mac") == H1_MAC
            and f.get("dst_mac") == H2_MAC
        ]
        if route_flows:
            route_dpids = sorted(set(f["dpid"] for f in route_flows))
            info(f"  Default-plane dpids for {H1_MAC}→{H2_MAC}: {route_dpids}\n")
            if route_dpids == [1, 2, 3]:
                info("PASS: route spans switches [1, 2, 3]\n")
            elif route_dpids == [1, 2]:
                info(
                    "FAIL: route missing sink switch s3 (expected [1,2,3], "
                    f"got {route_dpids})\n"
                )
                passed = False
            else:
                info(f"FAIL: unexpected route dpids {route_dpids} (expected [1,2,3])\n")
                passed = False
        else:
            info(f"FAIL: no default-plane flows found for {H1_MAC}→{H2_MAC}\n")
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
    info("\n--- Running Bug 1: Reversed LinkKey Order Test ---\n")
    test_reversed_link_order()
