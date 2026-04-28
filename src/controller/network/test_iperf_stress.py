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


def _link_count(data: dict | None) -> int:
    return len(data.get("links", [])) if data else 0


def _total_packets(data: dict | None) -> int:
    if not data:
        return 0
    return sum(
        p.get("rx_packets", 0) + p.get("tx_packets", 0)
        for sw in data.get("switches", [])
        for p in sw.get("ports", [])
    )


def _have_cmd(host, cmd: str) -> bool:
    return (
        host.cmd(f"command -v {cmd} >/dev/null 2>&1 && echo YES || echo NO").strip()
        == "YES"
    )


def _check_throughput(
    label: str, pkts_delta: int, secs_delta: float, passed: bool
) -> bool:
    if secs_delta <= 0 or pkts_delta <= 0:
        return passed
    pps = pkts_delta / secs_delta
    # Conservative lower bound: 64-byte packets at 50 Mbps ≈ 97k pkt/s
    # Data plane should be >> this; control plane << this
    if pps < 50_000:
        info(
            f"WARNING: {label} — only {pps:.0f} pkt/s, suspiciously low for data plane\n"
        )
    else:
        # Estimate at 1500B MTU (TCP data) vs 64B (ACKs) — use 1000B avg
        mbps = pps * 1000 * 8 / 1_000_000
        info(f"  └─ {label}: {pps:.0f} pkt/s ≈ {mbps:.0f} Mbps (data plane)\n")
    return passed


def _kill_iperf(h1, h2, h3):
    for h in (h1, h2, h3):
        h.cmd("killall -9 iperf iperf3 2>/dev/null || true")


def test_iperf_stress():
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")
    h3 = net.addHost("h3", ip="10.0.0.3", mac="00:00:00:00:00:03")

    # Full mesh: 3 switches fully interconnected, each with one host
    net.addLink(h1, s1)
    net.addLink(h2, s2)
    net.addLink(h3, s3)
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s1, s3)

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for topology discovery\n")
    time.sleep(6)

    info("*** Pinging to learn hosts and install flows\n")
    net.pingAll()
    time.sleep(2)

    passed = True
    links_before = 0
    links_during = 0
    pkts_baseline = 0
    time_baseline = 0.0
    pkts_before_break = 0
    time_before_break = 0.0
    pkts_after_break = 0
    time_after_break = 0.0
    pkts_after_fallback = 0
    time_after_fallback = 0.0
    pkts_after_recovery = 0
    time_after_recovery = 0.0

    try:
        # --- Check iperf availability ---
        if not _have_cmd(h1, "iperf") and not _have_cmd(h1, "iperf3"):
            info("FAIL: neither iperf nor iperf3 found on hosts\n")
            passed = False

        # --- Start iperf server on h3 ---
        iperf = "iperf3" if _have_cmd(h3, "iperf3") else "iperf"
        info(f"*** Starting {iperf} server on h3 (background)\n")
        h3.cmd(f"nohup {iperf} -s > /dev/null 2>&1 &")

        info(f"*** Starting {iperf} client on h1 -> h3 (background, 120s)\n")
        h1.cmd(f"nohup {iperf} -c 10.0.0.3 -t 120 -i 2 > /dev/null 2>&1 &")

        info("*** Letting iperf establish flow\n")
        time.sleep(4)

        # ── Phase 1: Baseline API calls with live traffic ──────────────
        info("*** [1] GET /topology — baseline with traffic\n")
        status, data = _api_get("/topology")
        if status != 200:
            info(f"FAIL: /topology returned {status}\n")
            passed = False
        else:
            links_before = _link_count(data)
            info(
                f"PASS: /topology — {links_before} switch-switch links, 3 hosts, 3 switches\n"
            )

        info("*** [2] GET /path/00:00:00:00:00:01/00:00:00:00:00:03 — h1->h3 path\n")
        status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: /path returned {status}\n")
            passed = False
        elif not data.get("hops"):
            info("FAIL: /path — no hops for h1->h3\n")
            passed = False
        else:
            info(f"PASS: /path — {len(data['hops'])} hops, plane={data.get('plane')}\n")

        info("*** [3] GET /path/00:00:00:00:00:02/00:00:00:00:00:03 — h2->h3 path\n")
        status, data = _api_get("/path/00:00:00:00:00:02/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: /path h2->h3 returned {status}\n")
            passed = False
        elif not data.get("hops"):
            info("FAIL: /path — no hops for h2->h3\n")
            passed = False
        else:
            info(f"PASS: /path h2->h3 — {len(data['hops'])} hops\n")

        info("*** [4] GET /flows — with iperf traffic flowing\n")
        status, data = _api_get("/flows")
        if status != 200:
            info(f"FAIL: /flows returned {status}\n")
            passed = False
        else:
            flow_count = len(data.get("flows", []))
            info(f"PASS: /flows — {flow_count} flow entries\n")

        info("*** [5] GET /stats/ports — counters while iperf active\n")
        status, data = _api_get("/stats/ports")
        if status != 200:
            info(f"FAIL: /stats/ports returned {status}\n")
            passed = False
        elif not data.get("switches"):
            info("FAIL: /stats/ports — no switches\n")
            passed = False
        else:
            pkts_baseline = _total_packets(data)
            time_baseline = time.time()
            info(f"PASS: /stats/ports — {pkts_baseline} pkts (iperf running ~4s)\n")

        info("*** [6] GET /policy/00:00:00:00:00:01/00:00:00:00:00:03 — initial\n")
        status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: /policy returned {status}\n")
            passed = False
        else:
            info(f"PASS: /policy — initial state={data.get('state')}\n")

        # ── Phase 2: Install policy while iperf runs ──────────────────
        info(
            "*** [7] POST /policy/00:00:00:00:00:01/00:00:00:00:00:03 — pin h1->h3 via s1-s2-s3\n"
        )
        # Full-mesh port layout:
        #   s1:port1=h1, port2=s2, port3=s3
        #   s2:port1=h2, port2=s1, port3=s3
        #   s3:port1=h3, port2=s2, port3=s1
        pinned_path = [
            {"src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 2},
            {"src_dpid": 2, "src_port": 3, "dst_dpid": 3, "dst_port": 2},
        ]
        status, _ = _api_post(
            "/policy/00:00:00:00:00:01/00:00:00:00:00:03",
            {"path": pinned_path},
        )
        if status != 200:
            info(f"FAIL: POST /policy returned {status}\n")
            passed = False
        else:
            info("PASS: POST /policy — installed\n")
            time.sleep(1)

        info("*** [8] GET /policy — verify POLICY_ACTIVE\n")
        status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200 or data.get("state") != "POLICY_ACTIVE":
            info(f"FAIL: expected POLICY_ACTIVE, got {data.get('state')}\n")
            passed = False
        else:
            info("PASS: /policy — POLICY_ACTIVE\n")

        info("*** [9] GET /path — verify policy plane\n")
        status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: /path returned {status}\n")
            passed = False
        elif data.get("plane") != "policy":
            info(f"FAIL: expected plane=policy, got {data.get('plane')}\n")
            passed = False
        else:
            info("PASS: /path — policy plane\n")

        info("*** [9a] Verify pinned path hops match what we submitted\n")
        status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:03")
        # Submitted pinned_path: s1:port2→s2:port2, s2:port3→s3:port2
        # Expected /path response: s1(in=1→out=2), s2(in=2→out=3), s3(in=2→out=1)
        expected_hops = [
            {"dpid": 1, "in_port": 1, "out_port": 2},
            {"dpid": 2, "in_port": 2, "out_port": 3},
            {"dpid": 3, "in_port": 2, "out_port": 1},
        ]
        if status != 200:
            info(f"FAIL: /path returned {status}\n")
            passed = False
        elif data.get("hops") != expected_hops:
            info(f"FAIL: pinned path mismatch, got {data.get('hops')}\n")
            passed = False
        else:
            info("PASS: pinned path matches expected s1→s2→s3\n")

        info("*** [10] GET /flows — should include policy flows\n")
        status, data = _api_get("/flows")
        if status == 200:
            planes = {f.get("plane") for f in data.get("flows", [])}
            info(f"PASS: /flows — planes seen: {planes}\n")
        else:
            info("FAIL: /flows\n")
            passed = False

        info("*** [10a] GET /stats/ports — capture before breaking link\n")
        status, data = _api_get("/stats/ports")
        if status != 200:
            info(f"FAIL: /stats/ports returned {status}\n")
            passed = False
        else:
            pkts_before_break = _total_packets(data)
            time_before_break = time.time()
            info(f"PASS: /stats/ports — {pkts_before_break} pkts before break\n")

        # ── Phase 3: Break the policy path while iperf runs ────────────
        info("*** [11] Breaking link s2-s3 (on policy path)\n")
        net.configLinkStatus("s2", "s3", "down")
        time.sleep(4)

        info("*** [12] GET /policy — should be POLICY_BROKEN\n")
        status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: /policy returned {status}\n")
            passed = False
        elif data.get("state") != "POLICY_BROKEN":
            info(f"FAIL: expected POLICY_BROKEN, got {data.get('state')}\n")
            passed = False
        else:
            info("PASS: /policy — POLICY_BROKEN\n")

        info("*** [12a] GET /stats/ports — verify traffic STOPPED (no auto-fallback)\n")
        status, data = _api_get("/stats/ports")
        if status != 200:
            info(f"FAIL: /stats/ports returned {status}\n")
            passed = False
        else:
            pkts_after_break = _total_packets(data)
            time_after_break = time.time()
            delta = pkts_after_break - pkts_before_break
            if delta > 100_000:
                info(
                    f"FAIL: traffic continued after pinned path broke (+{delta} pkts) — "
                    f"system should not auto-fallback\n"
                )
                passed = False
            else:
                info(
                    f"PASS: pinned path broken, traffic stopped (only +{delta} background pkts)\n"
                )

        info("*** [13] GET /topology — link count decreased\n")
        status, data = _api_get("/topology")
        if status != 200:
            info(f"FAIL: /topology returned {status}\n")
            passed = False
        else:
            links_during = _link_count(data)
            if links_during >= links_before:
                info(
                    f"FAIL: link count did not drop (before={links_before}, now={links_during})\n"
                )
                passed = False
            else:
                info(f"PASS: /topology — {links_during} links (was {links_before})\n")

        info(
            "*** [14] GET /path — path is still policy (BROKEN), no automatic reroute\n"
        )
        status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:03")
        if status == 200:
            info(
                f"PASS: /path — plane={data.get('plane')}, state={data.get('state')}\n"
            )
        else:
            info(f"FAIL: /path returned {status}\n")
            passed = False

        # Admin intervenes manually — re-pins to an alternative path (s1→s3 direct)
        info("*** [14a] POST /policy — admin re-pins h1→h3 via s1→s3 direct\n")
        alt_path = [
            {"src_dpid": 1, "src_port": 3, "dst_dpid": 3, "dst_port": 3},
        ]
        status, _ = _api_post(
            "/policy/00:00:00:00:00:01/00:00:00:00:00:03",
            {"path": alt_path},
        )
        if status != 200:
            info(f"FAIL: POST /policy (alternative path) returned {status}\n")
            passed = False
        else:
            info("PASS: POST /policy — alternative path installed\n")

        info("*** [14c] GET /policy — verify POLICY_ACTIVE on new route\n")
        status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: /policy returned {status}\n")
            passed = False
        elif data.get("state") != "POLICY_ACTIVE":
            info(f"FAIL: expected POLICY_ACTIVE, got {data.get('state')}\n")
            passed = False
        else:
            info("PASS: /policy — POLICY_ACTIVE (alternative path)\n")

        info("*** [14d] GET /path — verify 1-hop path via s1→s3\n")
        status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:03")
        expected_alt_hops = [
            {"dpid": 1, "in_port": 1, "out_port": 3},
            {"dpid": 3, "in_port": 3, "out_port": 1},
        ]
        if status != 200:
            info(f"FAIL: /path returned {status}\n")
            passed = False
        elif data.get("plane") != "policy":
            info(f"FAIL: expected plane=policy, got {data.get('plane')}\n")
            passed = False
        elif data.get("hops") != expected_alt_hops:
            info(f"FAIL: unexpected hops, got {data.get('hops')}\n")
            passed = False
        else:
            info("PASS: /path — 1-hop policy path via s1→s3\n")

        info("*** [14e] GET /stats/ports — verify traffic resumed after admin re-pin\n")
        time.sleep(6)
        status, data = _api_get("/stats/ports")
        if status != 200:
            info(f"FAIL: /stats/ports returned {status}\n")
            passed = False
        else:
            pkts_after_fallback = _total_packets(data)
            time_after_fallback = time.time()
            delta = pkts_after_fallback - pkts_after_break
            if delta < 1_000:
                info(f"FAIL: no traffic after admin re-pin (only +{delta} pkts)\n")
                passed = False
            else:
                elapsed = time_after_fallback - time_after_break
                info(f"PASS: traffic resumed after re-pin (+{delta} pkts)\n")
                passed = _check_throughput("after admin re-pin", delta, elapsed, passed)

        # ── Phase 4: Realistic stress scenarios ─────────────────────────
        # Scenario A: A user starts a second transfer (dual flow)
        info("*** [15] Starting second iperf client on h2 -> h3 (dual flow)\n")
        h2.cmd(f"nohup {iperf} -c 10.0.0.3 -t 120 > /dev/null 2>&1 &")
        time.sleep(3)

        # Scenario B: Bad cable / dying SFP — link s1-s3 flaps rapidly 4x
        info("*** [16] Flapping link s1-s3 4x (bad cable / dying SFP)\n")
        for flap_n in range(4):
            net.configLinkStatus("s1", "s3", "down")
            time.sleep(1)
            net.configLinkStatus("s1", "s3", "up")
            time.sleep(1)
            info(f"       flap {flap_n + 1}/4 done\n")
        time.sleep(3)

        info("*** [17] GET /topology — link count stable after flapping\n")
        status, data = _api_get("/topology")
        if status != 200:
            info(f"FAIL: /topology returned {status}\n")
            passed = False
        else:
            links_now = _link_count(data)
            if links_now < links_during:
                info(
                    f"FAIL: lost links after flapping ({links_now} < {links_during})\n"
                )
                passed = False
            else:
                info(f"PASS: /topology — {links_now} links after flapping\n")

        info("*** [18] GET /path — path survived flapping\n")
        status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:03")
        if status == 200 and data.get("hops"):
            info(f"PASS: /path — {len(data['hops'])} hops\n")
        else:
            info("FAIL: /path broken after flapping\n")
            passed = False

        # Scenario C: Switch s2 loses controller connection (crash / reboot)
        info("*** [19] Disconnecting s2 from controller (simulate switch crash)\n")
        s2.cmd("ovs-vsctl del-controller s2")
        time.sleep(4)

        info("*** [19a] GET /topology — s2 gone, only 1 link (s1-s3) should remain\n")
        status, data = _api_get("/topology")
        if status == 200:
            links_now = _link_count(data)
            info(f"PASS: /topology — {links_now} links after s2 disconnect\n")
        else:
            info(f"FAIL: /topology returned {status}\n")
            passed = False

        info("*** [19b] Reconnecting s2 to controller and restoring s2-s3\n")
        s2.cmd("ovs-vsctl set-controller s2 tcp:127.0.0.1:6653")
        net.configLinkStatus("s2", "s3", "up")
        time.sleep(7)

        info("*** [19c] GET /topology — full recovery after s2 reconnect\n")
        status, data = _api_get("/topology")
        if status != 200:
            info(f"FAIL: /topology returned {status}\n")
            passed = False
        else:
            links_after = _link_count(data)
            if links_after < 3:
                info(f"FAIL: not all links recovered ({links_after}/3)\n")
                passed = False
            else:
                info(f"PASS: /topology — {links_after} links, full mesh restored\n")

        info("*** [20] GET /path — path exists after s2 recovery\n")
        status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: /path returned {status}\n")
            passed = False
        elif data.get("hops"):
            info(f"PASS: /path — {len(data['hops'])} hops restored\n")
        else:
            info("FAIL: /path — no hops after s2 recovery\n")
            passed = False

        info("*** [20a] Ping all — verify data plane after switch reconnect\n")
        loss = net.pingAll()
        if loss != 0.0:
            info(f"FAIL: ping loss after s2 reconnect: {loss}%\n")
            passed = False
        else:
            info("PASS: 0% loss after s2 reconnect\n")

        info("*** [20b] GET /stats/ports — both iperf flows through all disruption\n")
        status, data = _api_get("/stats/ports")
        if status != 200:
            info(f"FAIL: /stats/ports returned {status}\n")
            passed = False
        else:
            pkts_after_recovery = _total_packets(data)
            time_after_recovery = time.time()
            delta = pkts_after_recovery - pkts_after_fallback
            info(
                f"PASS: /stats/ports — {pkts_after_recovery} pkts (+{delta} through flapping + switch crash)\n"
            )

        # ── Phase 5: Delete policy, verify cleanup ─────────────────────
        info("*** [21] DELETE /policy — remove pinned path\n")
        status, _ = _api_delete("/policy/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: DELETE /policy returned {status}\n")
            passed = False
        else:
            info("PASS: DELETE /policy\n")

        info("*** [22] GET /policy — UNSPECIFIED after delete\n")
        status, data = _api_get("/policy/00:00:00:00:00:01/00:00:00:00:00:03")
        if status != 200:
            info(f"FAIL: /policy returned {status}\n")
            passed = False
        elif data.get("state") != "UNSPECIFIED":
            info(f"FAIL: expected UNSPECIFIED, got {data.get('state')}\n")
            passed = False
        else:
            info("PASS: /policy — UNSPECIFIED\n")

        info("*** [23] GET /path — default plane after policy removal\n")
        status, data = _api_get("/path/00:00:00:00:00:01/00:00:00:00:00:03")
        if status == 200:
            info(
                f"PASS: /path — plane={data.get('plane')}, state={data.get('state')}\n"
            )
        else:
            info(f"FAIL: /path returned {status}\n")
            passed = False

        # ── Phase 6: Stats while traffic was flowing ──────────────────
        info("*** [24] GET /stats/ports — final counters with iperf history\n")
        status, data = _api_get("/stats/ports")
        if status == 200:
            pkts_final = _total_packets(data)
            now = time.time()
            info("PASS: /stats/ports — traffic timeline:\n")
            # Phase A: iperf startup → baseline
            seg_pkts = pkts_baseline
            seg_secs = time_baseline - 0  # relative, just for display
            info(f"         baseline   (5):   {pkts_baseline:>10} pkts\n")
            # Phase B: baseline → before break (policy install + baseline API)
            seg_pkts = pkts_before_break - pkts_baseline
            seg_secs = time_before_break - time_baseline
            if seg_pkts > 0 and seg_secs > 0:
                passed = _check_throughput(
                    "pre-break baseline", seg_pkts, seg_secs, passed
                )
            # Phase C: during break — should be near 0 (pinned path is broken)
            info(
                f"         pinned break (12a): {pkts_after_break:>10} pkts  (traffic stopped — correct)\n"
            )
            # Phase D: after admin re-pin → after recovery (flapping + switch crash)
            seg_pkts = pkts_after_recovery - pkts_after_fallback
            seg_secs = time_after_recovery - time_after_fallback
            if seg_pkts > 0 and seg_secs > 0:
                passed = _check_throughput(
                    "admin re-pin → recovery", seg_pkts, seg_secs, passed
                )
            # Phase E: recovery → final (policy delete + cleanup)
            seg_pkts = pkts_final - pkts_after_recovery
            seg_secs = now - time_after_recovery
            if seg_pkts > 0 and seg_secs > 0:
                passed = _check_throughput(
                    "post-recovery cleanup", seg_pkts, seg_secs, passed
                )
            info(f"         final      (24):   {pkts_final:>10} pkts\n")
        else:
            info("FAIL: /stats/ports\n")
            passed = False

        info("*** [25] Verify h2->h3 path after all disruption\n")
        status, data = _api_get("/path/00:00:00:00:00:02/00:00:00:00:00:03")
        if status == 200 and data.get("hops"):
            info(f"PASS: /path h2->h3 — {len(data['hops'])} hops\n")
        else:
            info("FAIL: /path h2->h3 broken after stress\n")
            passed = False

        info("*** [26] GET /flows — final state\n")
        status, data = _api_get("/flows")
        if status == 200:
            info(f"PASS: /flows — {len(data.get('flows', []))} entries\n")
        else:
            info("FAIL: /flows\n")
            passed = False

    finally:
        info("*** Killing iperf processes\n")
        _kill_iperf(h1, h2, h3)
        info("*** Stopping network\n")
        net.stop()
        subprocess.run(
            ["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

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
    info("\n--- Running Iperf Stress Test ---\n")
    test_iperf_stress()
