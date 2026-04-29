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


def test_policy_link_recovery():
    """
    Edge Case: Pinned path breaks then the underlying link comes back.
    Does the policy auto-recover to ACTIVE, or stay BROKEN?

    Topology: Ring  h1 — s1 — s2 — s3 — h2  (backup s1—s3)

    Phase 1: Install policy pinning h1→h2 via s1→s2→s3. Verify ACTIVE.
    Phase 2: Break s2-s3 on the pinned path. Verify BROKEN.
    Phase 3: Restore s2-s3. Check if the policy auto-recovered to ACTIVE.
    Phase 4: Ping to verify data plane works (via default routing).

    Per design: a pinned path should NOT auto-recover when a link comes
    back. The admin must explicitly re-pin or delete the policy. If the
    implementation auto-recovers, this is a bug (similar to the earlier
    auto-fallback issue).

    This catches bugs where:
    - The link-up handler in the fault handler accidentally restores
      policy state from BROKEN to ACTIVE
    - Old policy flows referencing the restored link are re-installed
      without admin intervention
    - PathComputer re-validates and reactivates stale policies
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
    info("*** Pinging to learn hosts\n")
    net.pingAll()
    time.sleep(2)

    passed = True

    # Install policy: h1→h2 via s1→s2→s3
    info("*** [1] POST policy pinning h1→h2 via s1→s2→s3\n")
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
    else:
        info("PASS: POST /policy\n")

    # Verify ACTIVE
    info("*** [2] Verify POLICY_ACTIVE\n")
    status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    if data.get("state") != "POLICY_ACTIVE":
        info(f"FAIL: expected POLICY_ACTIVE, got {data.get('state')}\n")
        passed = False
    else:
        info("PASS: POLICY_ACTIVE\n")

    # Break the middle link
    info("*** [3] Breaking s2-s3 (on pinned path)\n")
    net.configLinkStatus("s2", "s3", "down")
    time.sleep(4)

    # Verify BROKEN
    info("*** [4] Verify POLICY_BROKEN\n")
    status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    if data.get("state") != "POLICY_BROKEN":
        info(f"FAIL: expected POLICY_BROKEN, got {data.get('state')}\n")
        passed = False
    else:
        info("PASS: POLICY_BROKEN\n")

    # Restore the link
    info("*** [5] Restoring s2-s3\n")
    net.configLinkStatus("s2", "s3", "up")
    time.sleep(6)

    # Per design: a pinned path should NOT auto-recover. The admin must
    # explicitly re-pin or delete the policy.  After deletion the default
    # shortest path takes over and pings should succeed.
    info("*** [6] Check if policy auto-recovered after link restoration\n")
    status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    info(f"INFO: policy state after link restoration = {data.get('state')}\n")

    info("*** [7] DELETE policy (must remove BROKEN policy first)\n")
    status, _ = _api_delete("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: DELETE /policy returned {status}\n")
        passed = False
    else:
        info("PASS: DELETE /policy\n")

    # Ping should work now (default routing after policy removal)
    info("*** [8] Ping after recovery\n")
    loss = net.pingAll()
    if loss != 0.0:
        info(f"FAIL: ping loss: {loss}%\n")
        passed = False
    else:
        info("PASS: 0% loss\n")

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
    info("\n--- Running Policy Link Recovery Edge Case ---\n")
    test_policy_link_recovery()
