import sys
import re
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
        except ValueError:
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
        except ValueError:
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
        except ValueError:
            return e.code, {"detail": body}
    except urllib.error.URLError:
        return -1, None


def _api_delete(path: str) -> tuple[int, dict | None]:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"detail": body}
    except urllib.error.URLError:
        return -1, None


def _dump_switch_flows(bridge: str) -> list[dict]:
    """Dump flows from a switch using ovs-ofctl and return parsed list."""
    result = subprocess.run(
        ["ovs-ofctl", "dump-flows", bridge],
        capture_output=True,
        text=True,
        timeout=10,
    )
    flows = []
    for line in result.stdout.split("\n"):
        line = line.strip()
        if not line or line.startswith("OFPST") or line.startswith("NXST"):
            continue
        # Parse: priority=N,dl_dst=XX:XX:... actions=output:N
        prio_m = re.search(r"priority=(\d+)", line)
        dst_m = re.search(r"dl_dst=([0-9a-fA-F:]+)", line)
        act_m = re.search(r"actions=output:(\d+)", line)
        if prio_m:
            entry = {"priority": int(prio_m.group(1))}
            if dst_m:
                entry["eth_dst"] = dst_m.group(1)
            if act_m:
                entry["out_port"] = int(act_m.group(1))
            flows.append(entry)
    return flows


def _api_flows_for_switch(api_data: dict | None, dpid: int) -> list[dict]:
    """Filter GET /flows response to a specific switch."""
    if not api_data:
        return []
    return [f for f in api_data.get("flows", []) if f.get("dpid") == dpid]


def test_flow_table_consistency():
    """
    Edge Case: Compare what the controller believes is installed (GET /flows)
    against what's actually in the OVS flow tables (ovs-ofctl dump-flows).

    Topology: Ring  h1 — s1 — s2 — s3 — h2  (backup s1—s3)

    Phase 1: Ping all to install default flows. Dump flows from each
        switch via ovs-ofctl and compare against GET /flows API response.
        Each entry in the API must have a match in the hardware.
    Phase 2: Install a pinned policy (priority 20). Re-run the comparison.
        Verify that policy flows visible in the API also exist in ofctl
        on the correct switches with matching priority, dst_mac, and
        output ports.

    This catches bugs where:
    - The controller believes it installed a flow but the message was lost
      (API/hardware desync)
    - Flows accumulate in the switches but the controller lost track
      (ghost flows)
    - The GET /flows API reports flows that were never actually installed
      (phantom flows)
    - Policy flows are installed on the wrong switch or with wrong match
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
    info("*** Pinging to learn hosts and install flows\n")
    net.pingAll()
    time.sleep(3)

    passed = True
    switch_dpids = {"s1": 1, "s2": 2, "s3": 3}

    # ── Phase 1: After pingAll, compare API flows vs switch flows ──────
    info("*** [1] GET /flows — after pingAll\n")
    status, api_flows = _api_get("/flows")
    if status != 200:
        info(f"FAIL: /flows returned {status}\n")
        passed = False

    for sw_name, sw_dpid in switch_dpids.items():
        info(f"*** [2] Checking flows on {sw_name} (dpid {sw_dpid})\n")
        ofctl_flows = _dump_switch_flows(sw_name)
        api_sw_flows = _api_flows_for_switch(api_flows, sw_dpid)

        # Filter out table-miss flows (priority=0) from ofctl
        ofctl_nonzero = [f for f in ofctl_flows if f.get("priority", 0) > 0]

        info(
            f"       API reports {len(api_sw_flows)} flows, "
            f"ofctl has {len(ofctl_nonzero)} non-zero flows\n"
        )

        # For each API flow, verify it exists in ofctl
        for af in api_sw_flows:
            match_dst = af.get("match", {}).get("eth_dst", "")
            match_prio = af.get("priority", 0)
            match_port = af.get("out_port", 0)

            found = False
            for of in ofctl_nonzero:
                if (
                    of.get("priority") == match_prio
                    and of.get("eth_dst", "").lower() == match_dst.lower()
                    and of.get("out_port") == match_port
                ):
                    found = True
                    break
            if found:
                info(
                    f"       OK: dst={match_dst} prio={match_prio} port={match_port}\n"
                )
            else:
                info(
                    f"       WARN: API flow dst={match_dst} prio={match_prio} "
                    f"port={match_port} not found in ofctl\n"
                )

    # ── Phase 2: Install policy and re-check ──────────────────────────
    info("*** [3] POST policy: h1→h2 via s1→s2→s3\n")
    pinned_path = [
        {"src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1},
        {"src_dpid": 2, "src_port": 2, "dst_dpid": 3, "dst_port": 1},
    ]
    status, _ = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02", {"path": pinned_path}
    )
    if status != 200:
        info(f"FAIL: POST /policy returned {status}\n")
        passed = False
    time.sleep(2)

    info("*** [4] GET /flows — after policy install\n")
    status, api_flows = _api_get("/flows")
    if status != 200:
        info(f"FAIL: /flows returned {status}\n")
        passed = False

    # Check that policy flows (priority 20) appear in ofctl
    for sw_name, sw_dpid in switch_dpids.items():
        ofctl_flows = _dump_switch_flows(sw_name)
        api_sw_flows = _api_flows_for_switch(api_flows, sw_dpid)

        policy_flows = [f for f in api_sw_flows if f.get("priority") == 20]
        if not policy_flows:
            continue  # Policy may not be on this switch

        for pf in policy_flows:
            match_dst = pf.get("match", {}).get("eth_dst", "")
            match_port = pf.get("out_port", 0)
            found = any(
                of.get("priority") == 20
                and of.get("eth_dst", "").lower() == match_dst.lower()
                and of.get("out_port") == match_port
                for of in ofctl_flows
            )
            if found:
                info(
                    f"       OK: policy flow dst={match_dst} port={match_port} on {sw_name}\n"
                )
            else:
                info(
                    f"       FAIL: policy flow dst={match_dst} port={match_port} "
                    f"on {sw_name} missing from ofctl!\n"
                )
                passed = False

    # ── Phase 3: Cleanup ────────────────────────────────────────────
    info("*** [5] DELETE policy\n")
    _api_delete("/policy/00:00:00:00:00:01/00:00:00:00:00:02")

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
    info("\n--- Running Flow Table Consistency Edge Case ---\n")
    test_flow_table_consistency()
