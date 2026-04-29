import sys
import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def _move_host_ip(host, old_iface, new_iface, ip_cidr="10.0.0.1/8"):
    """Move a host's IP from one interface to another (simulates physical move)."""
    host.cmd(f"ip addr del {ip_cidr} dev {old_iface}")
    host.cmd(f"ip addr add {ip_cidr} dev {new_iface}")
    host.cmd(
        "arp -n | grep -v 10.0.0.1 | awk '{{print $1}}' | xargs -r -I{{}} arp -d {{}}"
    )


def _set_interface_mac(host, iface, mac):
    """Force an interface to use a specific MAC."""
    host.cmd(f"ip link set {iface} address {mac}")


def test_host_mobility_round_trip():
    """
    Edge Case: Round-trip mobility — can a host move, then move BACK?

    Topology: h1 connects to both s1 and s3 (same MAC, two links).
    h2 on s3. Backbone: s1 — s2 — s3.

        h1 — s1 — s2 — s3 — h2
         └───────────────┘

    Both h1 interfaces share the SAME MAC. The IP is moved between
    interfaces to simulate a physical device relocation.

    Phase 1: h1→h2 via s1 (IP on h1-eth0). h1 learned at s1.
    Phase 2: Kill h1-s1, move IP to h1-eth1 (s3). Purge h1 from s1.
    Phase 3: Ping via s3. h1 re-learned at s3.
    Phase 4: Restore h1-s1, kill h1-s3, move IP back to h1-eth0.
        Purge h1 from s3. This is the "return move."
    Phase 5: Ping via s1. h1 re-learned at s1 again — round trip.

    If Phase 5 fails, it means the second purge didn't happen or
    stale flows on s3 blackhole traffic destined for h1 at s1.
    """
    MAC_H1 = "00:00:00:00:00:01"

    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    h1 = net.addHost("h1", ip="10.0.0.1", mac=MAC_H1)
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")

    # Old location: h1 on s1
    net.addLink(h1, s1, intfName1="h1-eth0")
    # New location: h1 on s3
    net.addLink(h1, s3, intfName1="h1-eth1")
    # Backbone
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    # h2 on s3
    net.addLink(h2, s3)

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    # Ensure h1-eth1 uses the same MAC as h1-eth0
    _set_interface_mac(h1, "h1-eth1", MAC_H1)

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Baseline: h1→h2 via s1\n")
    loss_baseline = net.ping([h1, h2])

    # ── First move: s1 → s3 ─────────────────────────────────────────
    info("*** 2. MOVE 1: unplugging h1 from s1, moving to s3\n")
    net.configLinkStatus("h1", "s1", "down")
    time.sleep(2)
    _move_host_ip(h1, "h1-eth0", "h1-eth1")

    info("*** 3. Testing after move to s3\n")
    loss_move1 = net.ping([h1, h2])

    # ── Return move: s3 → s1 ────────────────────────────────────────
    info("*** 4. MOVE 2 (RETURN): restoring s1 link, unplugging from s3\n")
    net.configLinkStatus("h1", "s1", "up")
    time.sleep(2)
    net.configLinkStatus("h1", "s3", "down")
    time.sleep(2)
    _move_host_ip(h1, "h1-eth1", "h1-eth0")

    info("*** 5. Testing after move BACK to s1 (round-trip)\n")
    loss_move2 = net.ping([h1, h2])

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_baseline == 0.0 and loss_move1 == 0.0 and loss_move2 == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Baseline: {loss_baseline}%, Move1: {loss_move1}%, "
            f"Move2: {loss_move2}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Round-Trip Host Mobility Test ---\n")
    test_host_mobility_round_trip()
