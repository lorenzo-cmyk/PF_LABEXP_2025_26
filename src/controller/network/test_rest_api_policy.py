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
        except json.JSONDecodeError, ValueError:
            return e.code, {"detail": body}
    except urllib.error.URLError:
        return -1, None


def test_rest_api_policy():
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")

    # Ring topology: h1-s1, s1-s2, s2-s3, s3-h2, s1-s3
    net.addLink(h1, s1)
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s3, h2)
    net.addLink(s1, s3)

    net.build()
    net.start()

    info("*** Waiting for topology discovery\n")
    time.sleep(6)

    info("*** Pinging to trigger host learning\n")
    net.pingAll()
    time.sleep(2)

    passed = True

    # ── Test 1: Initial policy state is UNSPECIFIED ──────────────────
    info("*** 1. GET /policy/00:00:00:00:00:01/00:00:00:00:00:02 — initial state\n")
    status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: GET /policy returned {status}\n")
        passed = False
    elif data.get("state") != "UNSPECIFIED":
        info(f"FAIL: expected UNSPECIFIED, got {data.get('state')}\n")
        passed = False
    else:
        info("PASS: GET /policy — initial UNSPECIFIED\n")

    # ── Test 2: POST valid 2-hop policy path ─────────────────────────
    info(
        "*** 2. POST /policy/00:00:00:00:00:01/00:00:00:00:00:02 — install valid path\n"
    )
    valid_path = [
        {"src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1},
        {"src_dpid": 2, "src_port": 2, "dst_dpid": 3, "dst_port": 1},
    ]
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": valid_path},
    )
    if status != 200:
        info(f"FAIL: POST /policy returned {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — valid path installed\n")

    # ── Test 3: GET policy after install → POLICY_ACTIVE ────────────
    info("*** 3. GET /policy/00:00:00:00:00:01/00:00:00:00:00:02 — after install\n")
    status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: GET /policy returned {status}\n")
        passed = False
    elif data.get("state") != "POLICY_ACTIVE":
        info(f"FAIL: expected POLICY_ACTIVE, got {data.get('state')}\n")
        passed = False
    elif data.get("path") is None or len(data["path"]) != 2:
        info(f"FAIL: expected 2-hop path, got {data.get('path')}\n")
        passed = False
    else:
        info("PASS: GET /policy — POLICY_ACTIVE with path\n")

    # ── Test 4: GET /path shows policy plane ─────────────────────────
    info(
        "*** 4. GET /path/00:00:00:00:00:01/00:00:00:00:00:02 — should show policy plane\n"
    )
    status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: GET /path returned {status}\n")
        passed = False
    elif data.get("plane") != "policy":
        info(f"FAIL: expected plane=policy, got {data.get('plane')}\n")
        passed = False
    else:
        info("PASS: GET /path — policy plane active\n")

    # ── Test 5: DELETE policy ────────────────────────────────────────
    info("*** 5. DELETE /policy/00:00:00:00:00:01/00:00:00:00:00:02\n")
    status, data = _api_delete("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: DELETE /policy returned {status}\n")
        passed = False
    else:
        info("PASS: DELETE /policy\n")

    # ── Test 6: GET policy after delete → UNSPECIFIED ───────────────
    info("*** 6. GET /policy/00:00:00:00:00:01/00:00:00:00:00:02 — after delete\n")
    status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 200:
        info(f"FAIL: GET /policy returned {status}\n")
        passed = False
    elif data.get("state") != "UNSPECIFIED":
        info(f"FAIL: expected UNSPECIFIED after delete, got {data.get('state')}\n")
        passed = False
    else:
        info("PASS: GET /policy — UNSPECIFIED after delete\n")

    # ── Test 7: DELETE non-existent policy → 404 ─────────────────────
    info("*** 7. DELETE /policy (no active policy) — 404\n")
    status, _ = _api_delete("/policy/00:00:00:00:00:01/00:00:00:00:00:02")
    if status != 404:
        info(f"FAIL: expected 404 for deleting non-existent policy, got {status}\n")
        passed = False
    else:
        info("PASS: DELETE /policy — 404 on non-existent\n")

    # ── Test 8: POST invalid path (wrong port) → 400 ─────────────────
    info("*** 8. POST /policy — invalid path (wrong src_port) -> 400\n")
    bad_path = [
        {"src_dpid": 1, "src_port": 99, "dst_dpid": 2, "dst_port": 1},
    ]
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": bad_path},
    )
    if status != 400:
        info(f"FAIL: expected 400 for invalid path, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — invalid path returns 400\n")

    # ── Test 9: POST non-contiguous path → 400 ───────────────────────
    info("*** 9. POST /policy — non-contiguous path -> 400\n")
    non_contiguous = [
        {"src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1},
        {"src_dpid": 3, "src_port": 1, "dst_dpid": 2, "dst_port": 2},
    ]
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": non_contiguous},
    )
    if status != 400:
        info(f"FAIL: expected 400 for non-contiguous path, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — non-contiguous path returns 400\n")

    # ── Test 10: POST with same src/dst → 409 ────────────────────────
    info("*** 10. POST /policy — same src and dst MAC -> 409\n")
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:01",
        {"path": valid_path},
    )
    if status != 409:
        info(f"FAIL: expected 409 for same MAC, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — same MAC returns 409\n")

    # ── Test 11: GET /policy for unknown MAC → 404 ───────────────────
    info("*** 11. GET /policy — unknown MAC -> 404\n")
    status, _ = _api_get("/policy/00:00:00:00:00:ff/00:00:00:00:00:02")
    if status != 404:
        info(f"FAIL: expected 404 for unknown MAC, got {status}\n")
        passed = False
    else:
        info("PASS: GET /policy — unknown MAC returns 404\n")

    # ── Test 12: POST with empty path → 400 ──────────────────────────
    info("*** 12. POST /policy — empty path -> 400\n")
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": []},
    )
    if status != 400:
        info(f"FAIL: expected 400 for empty path, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — empty path returns 400\n")

    # ── Test 13: POST with missing 'path' key → 400 ──────────────────
    info("*** 13. POST /policy — missing path key -> 400\n")
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {},
    )
    if status != 400:
        info(f"FAIL: expected 400 for missing path, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — missing path key returns 400\n")

    # ── Test 14: POST with path=null → 400 ───────────────────────────
    info("*** 14. POST /policy — path=null -> 400\n")
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": None},
    )
    if status != 400:
        info(f"FAIL: expected 400 for path=null, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — path=null returns 400\n")

    # ── Test 15: POST with path as non-list string → 400/422 ─────────
    info("*** 15. POST /policy — path=string -> 400\n")
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": "not_a_list"},
    )
    if status not in (400, 422):
        info(f"FAIL: expected 400/422 for string path, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — string path returns error\n")

    # ── Test 16: POST with hop missing required fields → 400 ─────────
    info("*** 16. POST /policy — hop missing fields -> 400\n")
    missing_fields = [
        {"src_dpid": 1},  # missing src_port, dst_dpid, dst_port
    ]
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": missing_fields},
    )
    if status != 400:
        info(f"FAIL: expected 400 for missing hop fields, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — missing hop fields returns 400\n")

    # ── Test 17: POST with unparseable dpid value → 400 ──────────────
    info("*** 17. POST /policy — unparseable dpid -> 400\n")
    bad_value = [
        {"src_dpid": "not_a_dpid", "src_port": 1, "dst_dpid": 2, "dst_port": 1},
    ]
    status, data = _api_post(
        "/policy/00:00:00:00:00:01/00:00:00:00:00:02",
        {"path": bad_value},
    )
    if status != 400:
        info(f"FAIL: expected 400 for bad dpid value, got {status}: {data}\n")
        passed = False
    else:
        info("PASS: POST /policy — unparseable dpid returns 400\n")

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


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running REST API Policy CRUD Test ---\n")
    test_rest_api_policy()
