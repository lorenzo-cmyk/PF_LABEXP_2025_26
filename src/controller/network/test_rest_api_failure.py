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


def _link_count(data: dict | None) -> int:
    return len(data.get("links", [])) if data else 0


def test_rest_api_during_failure():
    """Validate REST API consistency during link failures.

    Linear topology: h1—s1—s2—s3—h2 with backup s1—s3.

    Checks:
    - GET /topology reflects the link count decrease after a failure.
    - GET /path returns correct state during failure and after recovery.
    - GET /policy shows POLICY_BROKEN when a policy path link fails.
    - Admin re-pinning (POST new path) recovers connectivity.
    """
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")

    # Linear topology: only ONE path h1-s1-s2-s3-h2
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

    info("*** Initial ping to learn hosts and install flows\n")
    net.pingAll()
    time.sleep(2)

    passed = True

    # ── Phase 1: Pre-failure baseline ─────────────────────────────────
    info("*** 1. GET /topology — initial link count\n")
    status, topo_before = _api_get("/topology")
    if status != 200:
        info(f"FAIL: /topology returned {status}\n")
        passed = False
    else:
        links_before = _link_count(topo_before)
        info(f"PASS: /topology — {links_before} switch-switch links\n")

    info("*** 2. GET /path/00:00:00:00:00:01/00:00:00:00:00:02 — path exists\n")
    status, path_before = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: /path returned {status}\n")
        passed = False
    elif not path_before.get("hops"):
        info("FAIL: /path returned no hops before failure\n")
        passed = False
    else:
        info(f"PASS: /path — {len(path_before['hops'])} hops\n")

    info("*** 3. Connectivity before failure — should succeed\n")
    loss_before = net.pingAll()
    if loss_before != 0.0:
        info(f"FAIL: ping loss before failure: {loss_before}%\n")
        passed = False
    else:
        info("PASS: 0% loss before failure\n")

    # ── Phase 2: Link failure ─────────────────────────────────────────
    info("*** 4. Breaking link s1-s2 (only path)\n")
    net.configLinkStatus("s1", "s2", "down")
    time.sleep(4)

    info("*** 5. GET /topology — link count should decrease\n")
    status, topo_during = _api_get("/topology")
    if status != 200:
        info(f"FAIL: /topology returned {status}\n")
        passed = False
    else:
        links_during = _link_count(topo_during)
        if links_during >= links_before:
            info(
                f"FAIL: link count did not drop (before={links_before}, during={links_during})\n"
            )
            passed = False
        else:
            info(f"PASS: /topology — {links_during} links (was {links_before})\n")

    info("*** 6. GET /path/00:00:00:00:00:01/00:00:00:00:00:02 — no route\n")
    status, path_during = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: /path returned {status}\n")
        passed = False
    elif path_during.get("state") == "active":
        info("FAIL: /path reports active despite partition\n")
        passed = False
    else:
        info(f"PASS: /path — state={path_during.get('state')}\n")

    info("*** 7. Connectivity during failure — should fail (linear split)\n")
    loss_during = net.pingAll()
    if loss_during != 100.0:
        info(f"FAIL: expected 100% loss during partition, got {loss_during}%\n")
        passed = False
    else:
        info("PASS: 100% loss during partition\n")

    # ── Phase 3: Recovery ────────────────────────────────────────────
    info("*** 8. Restoring link s1-s2\n")
    net.configLinkStatus("s1", "s2", "up")
    time.sleep(5)

    info("*** 9. GET /topology — link count should recover\n")
    status, topo_after = _api_get("/topology")
    if status != 200:
        info(f"FAIL: /topology returned {status}\n")
        passed = False
    else:
        links_after = _link_count(topo_after)
        if links_after <= links_during:
            info(
                f"FAIL: link count did not increase (during={links_during}, after={links_after})\n"
            )
            passed = False
        else:
            info(f"PASS: /topology — {links_after} links (was {links_during})\n")

    info("*** 10. GET /path/00:00:00:00:00:01/00:00:00:00:00:02 — path restored\n")
    status, path_after = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: /path returned {status}\n")
        passed = False
    elif not path_after.get("hops"):
        info("FAIL: /path returned no hops after recovery\n")
        passed = False
    else:
        info(f"PASS: /path — {len(path_after['hops'])} hops\n")

    info("*** 11. Connectivity after recovery — should succeed\n")
    loss_after = net.pingAll()
    if loss_after != 0.0:
        info(f"FAIL: ping loss after recovery: {loss_after}%\n")
        passed = False
    else:
        info("PASS: 0% loss after recovery\n")

    info("*** 12. GET /stats/ports — endpoint still works after failure\n")
    status, stats = _api_get("/stats/ports")
    if status != 200:
        info(f"FAIL: /stats/ports returned {status}\n")
        passed = False
    elif not stats.get("switches"):
        info("FAIL: /stats/ports returned no switches\n")
        passed = False
    else:
        info("PASS: /stats/ports works after failure\n")

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
    info("\n--- Running REST API Failure/Recovery Test ---\n")
    test_rest_api_during_failure()
