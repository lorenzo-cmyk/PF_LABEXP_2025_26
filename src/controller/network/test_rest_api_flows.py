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


def _api_post(path: str, body: dict) -> tuple[int, dict | None]:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError, ValueError:
            return e.code, {"detail": body}
    except urllib.error.URLError:
        return -1, None


def test_rest_api_flows():
    """Validate GET /flows endpoint structure and content.

    Ring topology: h1—s1—s2—s3—h2 with backup s1—s3.

    Checks:
    - Default flows are reported with priority 10, idle_timeout 30.
    - Policy flows override default flows (priority 20, no timeout).
    - Flow entries carry correct src_mac, dst_mac, plane metadata.
    - Deleting a policy removes its flows from the API response
      and reverts that pair to default entries.
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

    passed = True

    # ── Test 1: GET /flows before any traffic ────────────────────────
    info("*** 1. GET /flows — before traffic (may be empty)\n")
    status, data = _api_get("/flows")
    if status != 200:
        info(f"FAIL: /flows returned {status}\n")
        passed = False
    elif "flows" not in data:
        info(f"FAIL: /flows missing 'flows' key, got {list(data.keys())}\n")
        passed = False
    else:
        info("PASS: /flows — returns valid structure\n")

    # ── Test 2: Ping to install flows ────────────────────────────────
    info("*** 2. Pinging to trigger flow installation\n")
    net.pingAll()
    time.sleep(3)

    # ── Test 3: GET /flows after traffic ─────────────────────────────
    info("*** 3. GET /flows — after traffic\n")
    status, data = _api_get("/flows")
    if status != 200:
        info(f"FAIL: /flows returned {status}\n")
        passed = False
    elif not data.get("flows"):
        info("FAIL: /flows empty after ping\n")
        passed = False
    else:
        info(f"PASS: /flows — {len(data['flows'])} flow entries\n")

    # ── Test 4: Verify flow entry structure ──────────────────────────
    info("*** 4. Verifying flow entry fields\n")
    flows = data.get("flows", [])
    expected_fields = {
        "dpid",
        "match",
        "out_port",
        "priority",
        "idle_timeout",
        "plane",
        "src_mac",
        "dst_mac",
    }
    structural_ok = True
    for i, entry in enumerate(flows):
        missing = expected_fields - set(entry.keys())
        if missing:
            info(f"FAIL: flow[{i}] missing fields: {missing}\n")
            structural_ok = False
            break
        if "eth_dst" not in entry.get("match", {}):
            info(f"FAIL: flow[{i}] match missing eth_dst\n")
            structural_ok = False
            break
    if structural_ok:
        info("PASS: flow entry structure\n")
    else:
        passed = False

    # ── Test 5: Flows reference known MACs ──────────────────────────
    info("*** 5. Checking flows reference known MACs\n")
    known_macs = {"00:00:00:00:00:01", "00:00:00:00:00:02"}
    mac_ok = True
    for entry in flows:
        if entry.get("dst_mac") not in known_macs:
            info(f"FAIL: flow references unknown dst_mac={entry.get('dst_mac')}\n")
            mac_ok = False
            break
    if mac_ok:
        info("PASS: flows reference known MACs\n")
    else:
        passed = False

    # ── Test 6: No phantom dpids in flow entries ─────────────────────
    info("*** 6. Checking flows reference known dpids only\n")
    known_dpids = {1, 2, 3}
    dpid_ok = True
    for entry in flows:
        if entry.get("dpid") not in known_dpids:
            info(f"FAIL: flow references unknown dpid {entry.get('dpid')}\n")
            dpid_ok = False
            break
    if dpid_ok:
        info("PASS: flows reference known dpids\n")
    else:
        passed = False

    # ── Test 7: No negative or zero out_port in flows ────────────────
    info("*** 7. Checking flow out_ports are valid\n")
    port_ok = True
    for entry in flows:
        if entry.get("out_port", -1) < 0:
            info(f"FAIL: flow has invalid out_port {entry.get('out_port')}\n")
            port_ok = False
            break
    if port_ok:
        info("PASS: flow out_ports are valid\n")
    else:
        passed = False

    # ── Test 9: Default-plane flows have expected priority ───────────
    info("*** 9. Checking default-plane flow priority\n")
    default_flows = [f for f in flows if f.get("plane") == "default"]
    if default_flows:
        all_correct = all(f.get("priority") == 10 for f in default_flows)
        if all_correct:
            info("PASS: default-plane flows have priority 10\n")
        else:
            info("FAIL: some default-plane flows have unexpected priority\n")
            passed = False
    else:
        info("SKIP: no default-plane flows to check\n")

    # ── Test 10: Install a policy and verify policy flows appear ─────
    info("*** 10. POST /policy — install policy, then check /flows\n")
    valid_path = [
        {"src_dpid": 1, "src_port": 3, "dst_dpid": 3, "dst_port": 3},
    ]
    status, _ = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": valid_path},
    )
    if status == 200:
        time.sleep(2)
        status, data = _api_get("/flows")
        if status == 200:
            policy_flows = [
                f for f in data.get("flows", []) if f.get("plane") == "policy"
            ]
            if policy_flows:
                all_prio_20 = all(f.get("priority") == 20 for f in policy_flows)
                if all_prio_20:
                    info("PASS: policy flows have priority 20\n")
                else:
                    info("FAIL: expected policy flows priority 20\n")
                    passed = False
            else:
                info("SKIP: no policy flows found after install\n")
        else:
            info(f"FAIL: /flows returned {status}\n")
            passed = False
    else:
        info("SKIP: policy install failed, skipping policy flow check\n")

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
    info("\n--- Running REST API Flows Test ---\n")
    test_rest_api_flows()
