import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_same_switch_and_cross_switch_mixed():
    """
    Edge Case: Same-switch fast path mixed with cross-switch routing.

    Topology: h1 and h2 on s1 (same switch), h3 on s3.
    Backbone: s1 — s2 — s3

        h1 ─┐
             ├── s1 — s2 — s3 — h3
        h2 ─┘

    The ForwardingPlane has a special case: when src_dpid == dst_dpid,
    it installs a direct flow without computing a path. This path is
    completely untested by any existing test.

    Phase 1: h1↔h2 (same switch). Exercises the src_dpid==dst_dpid
        fast path — no path_computer call, no route_tracker links,
        direct edge-port flow install.

    Phase 2: h1↔h3 (cross switch). Normal multi-hop path computation
        and reverse path installation.

    Phase 3: h2↔h3 (cross switch). Another multi-hop path.

    Phase 4: Kill s1-s2. Same-switch h1↔h2 must be COMPLETELY UNAFFECTED
        (flows are local to s1, no inter-switch links involved).
        Cross-switch h1↔h3 and h2↔h3 must reroute via... well, there's
        no alternate path, so they should fail. This verifies that the
        same-switch flows are truly independent of the inter-switch
        topology.

    Phase 5: Restore s1-s2. Cross-switch should recover.

    This catches bugs where:
    - Same-switch flow installation doesn't work (wrong edge port lookup)
    - Link failure cleanup accidentally deletes same-switch flows
      (delete_flows_for_mac matches on dst_mac even for local flows)
    - Route tracker incorrectly tracks same-switch "routes" as using links
    """
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")
    h3 = net.addHost("h3", ip="10.0.0.3", mac="00:00:00:00:00:03")

    # Two hosts on s1 (same-switch pair)
    net.addLink(h1, s1)
    net.addLink(h2, s1)
    # Linear backbone
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    # h3 on s3
    net.addLink(h3, s3)

    net.build()
    net.start()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Same-switch: h1↔h2 (exercises src_dpid==dst_dpid fast path)\n")
    loss_same = net.ping([h1, h2])

    info("*** 2. Cross-switch: h1↔h3 (normal multi-hop path)\n")
    loss_cross1 = net.ping([h1, h3])

    info("*** 3. Cross-switch: h2↔h3 (normal multi-hop path)\n")
    loss_cross2 = net.ping([h2, h3])

    info("*** 4. Kill backbone link s1-s2 (same-switch must survive!)\n")
    net.configLinkStatus("s1", "s2", "down")
    time.sleep(3)

    info("*** 5. Same-switch h1↔h2 must STILL work (no inter-switch dependency)\n")
    loss_same_after = net.ping([h1, h2])

    info("*** 6. Cross-switch h1↔h3 should fail (no alternate path)\n")
    loss_cross1_after = net.ping([h1, h3])

    info("*** 7. Restore s1-s2\n")
    net.configLinkStatus("s1", "s2", "up")
    time.sleep(4)

    info("*** 8. Full recovery\n")
    loss_final = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = (
        loss_same == 0.0
        and loss_cross1 == 0.0
        and loss_cross2 == 0.0
        and loss_same_after == 0.0  # Critical: same-switch must survive link failure
        and loss_cross1_after == 100.0  # Expected: partitioned
        and loss_final == 0.0
    )

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Same: {loss_same}%, Cross1: {loss_cross1}%, Cross2: {loss_cross2}%, "
            f"AfterSame: {loss_same_after}%, AfterCross1: {loss_cross1_after}%, "
            f"Final: {loss_final}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Same-Switch + Cross-Switch Mixed Edge Case ---\n")
    test_same_switch_and_cross_switch_mixed()
