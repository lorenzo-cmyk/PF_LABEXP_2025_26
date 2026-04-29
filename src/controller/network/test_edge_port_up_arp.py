import sys
import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_port_up_triggers_st_recompute():
    """
    Edge Case: Host re-discovery after port-up event.

    Verifies that after an edge port goes down (host purged) and then
    comes back up, traffic can resume once the host re-announces itself
    through its own outbound packet.

    The controller does NOT flood ARP requests.  A host that has been
    purged from the tracker is invisible until it sends a packet on its
    own initiative.  The warmup ping (h1→h2) forces h1 to send ARP,
    teaching the controller its new location.

    Topology: h1 -- s1 -- s2 -- s3 -- h2

    Phases:
    1. Baseline ping both directions.
    2. Bring h1-s1 down -- edge-port purge, host h1 forgotten.
    3. Bring h1-s1 up -- port re-added to graph.
    4. Warmup ping h1→h2 -- h1 sends ARP, controller re-learns h1.
    5. Ping h2→h1 (should succeed -- h1 is now known).
    6. Ping h1→h2 (should succeed -- unicast path works).
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

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for topology discovery\n")
    time.sleep(5)

    info("*** 1. Baseline ping (both directions)\n")
    loss_baseline = net.pingAll()

    info("*** 2. Bringing h1-s1 link DOWN (edge port purge + ST recompute)\n")
    net.configLinkStatus("h1", "s1", "down")
    time.sleep(3)

    info(
        "*** 3. Bringing h1-s1 link UP (port re-added, ST recomputed, flood rules refreshed)\n"
    )
    net.configLinkStatus("h1", "s1", "up")
    time.sleep(2)

    for h in [h1, h2]:
        h.cmd("ip neigh flush all 2>/dev/null")

    info("*** 4. Warmup: let h1 announce itself via its own traffic\n")
    h1.cmd(f"ping -c 1 -W 1 {h2.IP()} > /dev/null 2>&1")
    time.sleep(1)

    info("*** 5. Ping h2 -> h1 (should SUCCEED -- controller knows h1 now)\n")
    loss_h2_to_h1 = net.ping([h2, h1])

    info("*** 6. Ping h1 -> h2 (should SUCCEED -- unicast path still works)\n")
    loss_h1_to_h2 = net.ping([h1, h2])

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_baseline == 0.0 and loss_h1_to_h2 == 0.0 and loss_h2_to_h1 == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Baseline: {loss_baseline}%, h1->h2: {loss_h1_to_h2}%, "
            f"h2->h1: {loss_h2_to_h1}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Port UP ST Recompute Test ---\n")
    test_port_up_triggers_st_recompute()
