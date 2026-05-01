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


def test_rest_api_topology():
    """Validate GET /topology and GET /path endpoints.

    Ring topology: h1—s1—s2—s3—h2 with backup s1—s3.

    Checks:
    - /topology returns switches, links, hosts with correct structure.
    - /path returns hop chain for known hosts, 404 for unknown.
    - No phantom switches, hosts, or cross-switch link references.
    - Hop entries have valid dpids and non-negative port numbers.
    """
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")

    net.addLink(h1, s1)
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s3, h2)
    net.addLink(s1, s3)

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for topology discovery\n")
    time.sleep(6)

    info("*** Pinging to trigger host learning and flow installation\n")
    net.pingAll()
    time.sleep(2)

    passed = True

    info("*** 1. GET /topology — full snapshot\n")
    status, data = _api_get("/topology")
    if status != 200:
        info(f"FAIL: /topology returned status {status}\n")
        passed = False
    elif not all(k in data for k in ("switches", "links", "hosts")):
        info(f"FAIL: /topology missing keys, got {list(data.keys())}\n")
        passed = False
    elif sorted(s["dpid"] for s in data["switches"]) != [1, 2, 3]:
        info(
            f"FAIL: /topology switch dpids = {[s['dpid'] for s in data['switches']]}, expected [1,2,3]\n"
        )
        passed = False
    elif len(data["links"]) < 2:
        info(f"FAIL: /topology links = {len(data['links'])}, expected >= 2\n")
        passed = False
    elif len(data["hosts"]) != 2:
        info(f"FAIL: /topology hosts = {len(data['hosts'])}, expected 2\n")
        passed = False
    else:
        info("PASS: /topology\n")

    info(
        "*** 2. GET /path/00:00:00:00:00:01/00:00:00:00:00:02 — path between known hosts\n"
    )
    status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: /path returned status {status}\n")
        passed = False
    elif not all(k in data for k in ("src_mac", "dst_mac", "plane", "state", "hops")):
        info(f"FAIL: /path missing keys, got {list(data.keys())}\n")
        passed = False
    elif not data["hops"]:
        info("FAIL: /path returned empty hops list\n")
        passed = False
    else:
        info("PASS: /path — known hosts\n")

    info("*** 3. GET /path — verify hop structure\n")
    for i, hop in enumerate(data.get("hops", [])):
        if not all(k in hop for k in ("dpid", "in_port", "out_port")):
            info(f"FAIL: hop[{i}] missing key, got {list(hop.keys())}\n")
            passed = False
            break
    else:
        info("PASS: /path — hop structure\n")

    info(
        "*** 4. GET /path/00:00:00:00:00:ff/00:00:00:00:00:02 — unknown src MAC -> 404\n"
    )
    status, _ = _api_get("/path/00:00:00:00:00:ff/00:00:00:00:00:02")
    if status != 404:
        info(f"FAIL: expected 404 for unknown src MAC, got {status}\n")
        passed = False
    else:
        info("PASS: /path — unknown src MAC returns 404\n")

    info(
        "*** 5. GET /path/00:00:00:00:00:01/00:00:00:00:00:ff — unknown dst MAC -> 404\n"
    )
    status, _ = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:ff")
    if status != 404:
        info(f"FAIL: expected 404 for unknown dst MAC, got {status}\n")
        passed = False
    else:
        info("PASS: /path — unknown dst MAC returns 404\n")

    info("*** 6. /topology — no phantom hosts\n")
    status, data = _api_get("/topology")
    if status == 200:
        host_macs = {h["mac"] for h in data.get("hosts", [])}
        expected_macs = {"00:00:00:00:00:01", "00:00:00:00:00:02"}
        if host_macs != expected_macs:
            info(f"FAIL: unexpected hosts reported: {host_macs}\n")
            passed = False
        elif len(data["hosts"]) != 2:
            info(f"FAIL: expected 2 hosts, got {len(data['hosts'])}\n")
            passed = False
        else:
            info("PASS: /topology — no phantom hosts\n")

    info("*** 7. /topology — no phantom switches\n")
    if status == 200:
        switch_dpids = {s["dpid"] for s in data.get("switches", [])}
        if switch_dpids != {1, 2, 3}:
            info(f"FAIL: unexpected switch dpids reported: {switch_dpids}\n")
            passed = False
        else:
            info("PASS: /topology — no phantom switches\n")

    info("*** 8. /topology — links only reference known switches\n")
    if status == 200:
        known_dpids = {1, 2, 3}
        all_src_ok = all(lk["src_dpid"] in known_dpids for lk in data.get("links", []))
        all_dst_ok = all(lk["dst_dpid"] in known_dpids for lk in data.get("links", []))
        if not all_src_ok or not all_dst_ok:
            info("FAIL: link references unknown switch\n")
            passed = False
        else:
            info("PASS: /topology — links reference known switches\n")

    info("*** 9. /path hops — no unknown dpids or negative ports\n")
    status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:02")
    if status == 200:
        known_dpids = {1, 2, 3}
        hops_ok = True
        for hop in data.get("hops", []):
            if hop["dpid"] not in known_dpids:
                info(f"FAIL: hop references unknown dpid {hop['dpid']}\n")
                hops_ok = False
                break
            if hop["in_port"] < 0 or hop["out_port"] < 0:
                info(f"FAIL: hop has negative port in {hop}\n")
                hops_ok = False
                break
        if hops_ok:
            info("PASS: /path hops — valid dpids and ports\n")
        else:
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
    info("\n--- Running REST API Topology Test ---\n")
    test_rest_api_topology()
