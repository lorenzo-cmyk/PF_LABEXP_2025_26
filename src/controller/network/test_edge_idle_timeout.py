import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_flow_idle_timeout_and_reinstall():
    """
    Edge Case: Flows expire after 30s idle — controller must fully re-install.

    Topology: Linear h1 — s1 — s2 — s3 — h2

    FlowInstaller sets DEFAULT_IDLE_TIMEOUT=30. If no packet matches a
    flow for 30 seconds, the switch silently deletes it. The controller
    is NOT notified — there's no OpenFlow message for idle expiry.

    Phase 1: Ping h1↔h2. Controller installs flows on s1, s2, s3.
        Host tracker learns both MACs. Path computer caches the path.

    Phase 2: Wait 35 seconds. All flows expire silently in the switches.
        The controller still has:
        - Host tracker entries (MAC → location) ← still valid
        - Path computer cache ← may or may not still be there
        - Route tracker entries ← still present (no failure triggered cleanup)

    Phase 3: Ping h1↔h2 again. The packet hits table-miss on s1 and
        goes to the controller. The controller must:
        1. Look up src_mac and dst_mac in host tracker (both still valid)
        2. Compute path (may hit cache or recompute)
        3. Install fresh flows on all switches
        4. Send packet-out for the first packet

    The critical thing: the RouteTracker still thinks the old flows exist
    (they expired silently). When the controller calls add_route() again,
    it replaces the old entry — but if the old links don't match the
    new links (e.g., topology changed during the idle period), the
    RouteTracker index could get corrupted.

    This is the most "invisible" failure mode — everything looks correct
    in the controller, but the switches have empty flow tables.
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

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Establish flows with ping\n")
    loss_initial = net.ping([h1, h2])

    info("*** 2. Idle period: waiting 35 seconds for flows to expire (timeout=30s)\n")
    time.sleep(35)

    info("*** 3. Post-expiry ping (controller must re-install all flows)\n")
    loss_after_idle = net.ping([h1, h2])

    info("*** 4. Second ping (flows should be fresh now)\n")
    loss_reinstalled = net.ping([h1, h2])

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_initial == 0.0 and loss_after_idle == 0.0 and loss_reinstalled == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Initial: {loss_initial}%, AfterIdle: {loss_after_idle}%, "
            f"Reinstalled: {loss_reinstalled}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Flow Idle Timeout / Reinstall Edge Case ---\n")
    test_flow_idle_timeout_and_reinstall()
