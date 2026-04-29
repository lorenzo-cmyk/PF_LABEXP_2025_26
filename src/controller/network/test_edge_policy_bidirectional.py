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


def test_bidirectional_policy():
    """
    Edge Case: Policies are supposed to install flows in both directions.
    Verify that the reverse direction (h2→h1) mirrors the forward path.

    Topology: Ring  h1 — s1 — s2 — s3 — h2  (backup s1—s3)

    Phase 1: Install policy h1→h2 via s1→s2→s3.
    Phase 2: Verify forward path (/path h1→h2) shows policy plane.
    Phase 3: Verify reverse path (/path h2→h1) also shows policy plane
        with correctly reversed hops (s3→s2→s1).
    Phase 4: Break the middle link (s2-s3). Both directions must show
        POLICY_BROKEN — not just the forward direction.
    Phase 5: Delete policy, restore link, verify default routing works.

    This catches bugs where:
    - The reverse-direction policy is not installed (asymmetric policy)
    - Only the src→dst direction is tracked, dst→src falls back to default
    - Link failure cleanup only removes forward-path flows but leaves
      reverse-path flows in place (partial cleanup)
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

    info("*** [1] Install policy: h1→h2 via s1→s2→s3\n")
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
    time.sleep(1)

    info("*** [2] GET /path h1→h2 — should show policy plane\n")
    status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:02")
    if data.get("plane") != "policy":
        info(f"FAIL: forward plane={data.get('plane')}\n")
        passed = False
    else:
        info("PASS: forward path is policy plane\n")

    info("*** [3] GET /path h2→h1 — should also show policy plane (bidirectional)\n")
    status, data = _api_get("/path/00:00:00:00:00:02/00:00:00:00:00:01")
    if data.get("plane") != "policy":
        info(f"FAIL: reverse plane={data.get('plane')}\n")
        passed = False
    else:
        info("PASS: reverse path is policy plane\n")

    info("*** [4] Verify reverse path hops are the reverse of forward path\n")
    status, rev = _api_get("/path/00:00:00:00:00:02/00:00:00:00:00:01")
    rev_hops = rev.get("hops", [])
    # Reverse path should have 3 hops: s3→s2→s1 (with reversed in/out ports)
    expected_rev = [
        {"dpid": 3, "in_port": 2, "out_port": 1},
        {"dpid": 2, "in_port": 2, "out_port": 1},
        {"dpid": 1, "in_port": 2, "out_port": 1},
    ]
    if rev_hops != expected_rev:
        info(f"FAIL: reverse hops mismatch, got {rev_hops}\n")
        passed = False
    else:
        info("PASS: reverse path correctly reversed\n")

    # Break the middle link — should affect both directions
    info("*** [5] Breaking s2-s3 (on pinned path)\n")
    net.configLinkStatus("s2", "s3", "down")
    time.sleep(4)

    info("*** [6] GET /path h1→h2 — should be BROKEN\n")
    status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:02")
    if data.get("plane") == "policy" and data.get("state") == "POLICY_BROKEN":
        info("PASS: forward policy BROKEN\n")
    else:
        info(f"FAIL: forward plane={data.get('plane')}, state={data.get('state')}\n")
        passed = False

    info("*** [7] GET /path h2→h1 — should also be BROKEN\n")
    status, data = _api_get("/path/00:00:00:00:00:02/00:00:00:00:00:01")
    if data.get("plane") == "policy" and data.get("state") == "POLICY_BROKEN":
        info("PASS: reverse policy BROKEN\n")
    else:
        info(f"FAIL: reverse plane={data.get('plane')}, state={data.get('state')}\n")
        passed = False

    info("*** [8] DELETE policy\n")
    _api_delete("/policy/00:00:00:00:00:01/00:00:00:00:00:02")

    info("*** [9] Restore link and verify default routing\n")
    net.configLinkStatus("s2", "s3", "up")
    time.sleep(4)

    loss = net.pingAll()
    if loss != 0.0:
        info(f"FAIL: ping loss: {loss}%\n")
        passed = False
    else:
        info("PASS: 0% loss — connectivity restored\n")

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
    info("\n--- Running Bidirectional Policy Edge Case ---\n")
    test_bidirectional_policy()
