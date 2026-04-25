import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def _move_host_ip(host, old_iface, new_iface, ip_cidr="10.0.0.1/8"):
    """Move a host's IP from one interface to another (simulates physical move)."""
    host.cmd(f"ip addr del {ip_cidr} dev {old_iface}")
    host.cmd(f"ip addr add {ip_cidr} dev {new_iface}")
    host.cmd("arp -d 10.0.0.2 2>/dev/null")  # Flush peer ARP


def _set_interface_mac(host, iface, mac):
    """Force an interface to use a specific MAC."""
    host.cmd(f"ip link set {iface} address {mac}")


def test_host_mobility_edge_port_purge():
    """
    Edge Case: Single-port device mobility via edge-port-down purge.

    Topology: h1 connects to s1 AND s3 (same MAC on both links).
    h2 connects to s3. Inter-switch: s1-s2-s3.

        h1 — s1 — s2 — s3 — h2
         └───────────────┘

    This simulates a device physically unplugging from s1 and plugging
    into s3. Mininet can't add links at runtime, so both links are
    pre-positioned. The test manages IP/MAC to simulate a real move:

    - Both h1 interfaces share the SAME MAC (00:00:00:00:00:01)
    - The IP (10.0.0.1) is MOVED between interfaces during the move

    Phase 1 (baseline): IP on h1-eth0 (s1). Ping h1↔h2.
        Controller learns h1 at (s1, port-to-h1).

    Phase 2 (unplug from s1): Bring down h1-eth0. Move IP to h1-eth1.
        The FaultHandler sees edge port down → purges h1 from tracker.

    Phase 3 (reconnect at s3): Ping h1↔h2 via h1-eth1 (s3).
        Controller sees h1's MAC at s3 → re-learns h1 at (s3, port).
        New path installed. Ping succeeds.

    PASS: baseline=0%, after-move=0%.
    FAIL: If after-move>0%, the edge-port purge didn't happen.
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
    # New location: h1 on s3 (pre-positioned)
    net.addLink(h1, s3, intfName1="h1-eth1")
    # Inter-switch backbone
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    # h2 on s3
    net.addLink(h2, s3)

    net.build()
    net.start()

    # Ensure h1-eth1 uses the same MAC as h1-eth0
    _set_interface_mac(h1, "h1-eth1", MAC_H1)

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Baseline: h1→h2 via s1\n")
    loss_baseline = net.ping([h1, h2])

    info("*** 2. Unplugging h1 from s1 (edge port-down → purge h1)\n")
    net.configLinkStatus("h1", "s1", "down")
    time.sleep(2)

    # Move IP from h1-eth0 to h1-eth1 (simulates physical move)
    _move_host_ip(h1, "h1-eth0", "h1-eth1")

    info("*** 3. Testing mobility: h1→h2 after moving to s3\n")
    loss_after_move = net.ping([h1, h2])

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_baseline == 0.0 and loss_after_move == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Baseline: {loss_baseline}%, AfterMove: {loss_after_move}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Host Mobility / Edge Port Purge Edge Case ---\n")
    test_host_mobility_edge_port_purge()
