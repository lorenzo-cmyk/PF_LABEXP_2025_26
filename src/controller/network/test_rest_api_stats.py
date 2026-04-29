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


def test_rest_api_stats():
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

    info("*** Pinging to generate traffic for port counters\n")
    net.pingAll()
    time.sleep(3)

    passed = True

    info("*** 1. GET /stats/ports — returns 200\n")
    status, data = _api_get("/stats/ports")
    if status != 200:
        info(f"FAIL: /stats/ports returned {status}\n")
        passed = False
    elif "switches" not in data:
        info("FAIL: /stats/ports missing 'switches' key\n")
        passed = False
    else:
        info("PASS: /stats/ports returns 200\n")

    info("*** 2. All 3 switches reported\n")
    switches = data.get("switches", [])
    dpid_set = {s["dpid"] for s in switches}
    if dpid_set != {1, 2, 3}:
        info(f"FAIL: expected dpids {{1,2,3}}, got {dpid_set}\n")
        passed = False
    else:
        info("PASS: all 3 switches present\n")

    info("*** 3. Each switch has at least one port\n")
    if any(len(s.get("ports", [])) == 0 for s in switches):
        info("FAIL: some switches have no ports\n")
        passed = False
    else:
        info("PASS: each switch has ports\n")

    info("*** 4. Port entries have all required fields\n")
    expected_port_keys = {
        "port_no",
        "rx_packets",
        "tx_packets",
        "rx_bytes",
        "tx_bytes",
        "rx_dropped",
        "tx_dropped",
        "rx_errors",
        "tx_errors",
        "last_updated",
    }
    port_struct_ok = True
    for sw in switches:
        for port in sw.get("ports", []):
            missing = expected_port_keys - set(port.keys())
            if missing:
                info(
                    f"FAIL: switch {sw['dpid']} port {port.get('port_no')} missing: {missing}\n"
                )
                port_struct_ok = False
                break
        if not port_struct_ok:
            break
    if port_struct_ok:
        info("PASS: all port fields present\n")
    else:
        passed = False

    info("*** 5. Port counters are non-negative\n")
    counter_keys = [
        "rx_packets",
        "tx_packets",
        "rx_bytes",
        "tx_bytes",
        "rx_dropped",
        "tx_dropped",
        "rx_errors",
        "tx_errors",
    ]
    counters_ok = True
    for sw in switches:
        for port in sw.get("ports", []):
            for key in counter_keys:
                val = port.get(key, -1)
                if not isinstance(val, int) or val < 0:
                    info(
                        f"FAIL: switch {sw['dpid']} port {port['port_no']} {key}={val}\n"
                    )
                    counters_ok = False
                    break
            if not counters_ok:
                break
        if not counters_ok:
            break
    if counters_ok:
        info("PASS: port counters are non-negative\n")
    else:
        passed = False

    info("*** 6. last_updated is a recent timestamp\n")
    now = time.time()
    time_ok = True
    for sw in switches:
        for port in sw.get("ports", []):
            ts = port.get("last_updated", 0)
            if not isinstance(ts, (int, float)) or ts <= 0 or ts > now + 1:
                info(
                    f"FAIL: switch {sw['dpid']} port {port['port_no']} invalid ts={ts}\n"
                )
                time_ok = False
                break
        if not time_ok:
            break
    if time_ok:
        info("PASS: last_updated is valid\n")
    else:
        passed = False

    info("*** 7. Traffic counters increased after ping\n")

    def _total_packets(data):
        total = 0
        for sw in data.get("switches", []):
            for port in sw.get("ports", []):
                total += port.get("rx_packets", 0) + port.get("tx_packets", 0)
        return total

    packets_before = _total_packets(data)
    net.pingAll()
    info("*** Waiting for stats poll interval (5s)\n")
    time.sleep(6)
    status, data2 = _api_get("/stats/ports")
    if status != 200:
        info("FAIL: second /stats/ports call failed\n")
        passed = False
    else:
        packets_after = _total_packets(data2)
        if packets_after <= packets_before:
            info(
                f"FAIL: packet count did not increase (before={packets_before}, after={packets_after})\n"
            )
            passed = False
        else:
            info("PASS: traffic counters increased\n")

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
    info("\n--- Running REST API Port Stats Test ---\n")
    test_rest_api_stats()
