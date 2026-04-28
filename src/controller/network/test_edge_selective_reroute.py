import sys
import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_selective_reroute_multi_pair():
    r"""
    Edge Case: RouteTracker multi-pair isolation during link failure.

    Topology:
              s2
            /    \
        h1-s1    s4-h2
            \    /
              s3
              |
              h3

    Three hosts generate three independent pairs: h1↔h2, h1↔h3, h2↔h3.
    The RouteTracker must track all pairs and their links independently.

    Phase 1: All-to-all ping. Installs 6 directional routes (3 pairs × 2 dirs).

    Phase 2: Kill link s3-s4. This should only affect routes that traverse
        that link (h2↔h3 and possibly h1↔h3 depending on path selection).
        The h1↔h2 pair routes entirely through s1-s2-s4 and should be
        COMPLETELY UNTOUCHED — no flow deletion, no re-ARP, no packet loss.

    Phase 3: Restore s3-s4. Verify full recovery.

    This test catches RouteTracker bugs where:
    - pairs_on_link() returns too many or too few pairs
    - flow deletion for one pair accidentally deletes another pair's flows
      (e.g., delete_flows_for_mac on a shared MAC corrupts unrelated flows)
    - route removal for one pair corrupts the link→pair index for others
    """
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")
    s4 = net.addSwitch("s4", dpid="0000000000000004")

    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")
    h3 = net.addHost("h3", ip="10.0.0.3", mac="00:00:00:00:00:03")

    net.addLink(h1, s1)
    net.addLink(h2, s4)
    net.addLink(h3, s3)

    # Diamond between s1 and s4, plus h3 hanging off s3
    net.addLink(s1, s2)
    net.addLink(s2, s4)
    net.addLink(s1, s3)
    net.addLink(s3, s4)

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. All-to-all baseline\n")
    loss_baseline = net.pingAll()

    info("*** 2. Establish individual pair flows with targeted pings\n")
    # Ensure all 3 pairs have installed routes (pingAll does this, but
    # targeted pings make the intent explicit and ensure flows are fresh)
    loss_12 = net.ping([h1, h2])
    loss_13 = net.ping([h1, h3])
    loss_23 = net.ping([h2, h3])

    info("*** 3. Kill link s3-s4 (should NOT affect h1↔h2 via s1-s2-s4)\n")
    net.configLinkStatus("s3", "s4", "down")
    time.sleep(3)

    info("*** 4. h1↔h2 must survive untouched (flows on s1,s2,s4 unaffected)\n")
    loss_12_after = net.ping([h1, h2])

    info("*** 5. h1↔h3 may need reroute (was possibly via s3-s4)\n")
    loss_13_after = net.ping([h1, h3])

    info("*** 6. h2↔h3 may need reroute (h2 on s4, h3 on s3, link s3-s4 is down)\n")
    loss_23_after = net.ping([h2, h3])

    info("*** 7. Restore s3-s4\n")
    net.configLinkStatus("s3", "s4", "up")
    time.sleep(4)

    info("*** 8. Final all-to-all\n")
    loss_final = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # h1↔h2 MUST survive (zero loss). h1↔h3 and h2↔h3 should recover after reroute.
    passed = (
        loss_baseline == 0.0
        and loss_12 == 0.0
        and loss_13 == 0.0
        and loss_23 == 0.0
        and loss_12_after == 0.0  # Critical: unaffected pair must survive
        and loss_13_after == 0.0
        and loss_23_after == 0.0
        and loss_final == 0.0
    )

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Baseline: {loss_baseline}%, 12: {loss_12}%, 13: {loss_13}%, "
            f"23: {loss_23}%, After12: {loss_12_after}%, After13: {loss_13_after}%, "
            f"After23: {loss_23_after}%, Final: {loss_final}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Selective Reroute / Multi-Pair Edge Case ---\n")
    test_selective_reroute_multi_pair()
