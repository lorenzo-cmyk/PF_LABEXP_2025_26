import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_host_relocation_immutable_tracker():
    """
    Edge Case: Host physically moves — the "first observation wins" rule blocks re-learning.

    Topology: h1 is dual-homed to s1 AND s3 (two interfaces).
    h2 connects to s3.

        h1(if1) — s1 — s2 — s3 — h2
        h1(if2) ─────────────┘

    Phase 1: h1 pings h2 via if1 (through s1). The controller learns
    h1 at (s1, port-to-h1) and installs paths.

    Phase 2: We bring down h1's link to s1. Now h1 can only reach the
    network via if2 → s3. When h1 sends an ARP/ping through s3, the
    host tracker sees h1's MAC on (s3, port-to-h1) but REFUSES to
    update (first observation wins, GOAL 1: no mobility).

    Expected outcome: h2→h1 traffic still routes to s1's dead port
    because the controller believes h1 is at s1. The ping should FAIL.

    This test deliberately BREAKS the controller to demonstrate the
    immutability invariant. A PASS means the controller correctly
    refuses to relocate the host (and thus ping fails as designed).
    If ping somehow succeeds, it means the immutability rule was
    violated — which is itself a bug.
    """
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    # h1 uses only one MAC but has two physical links
    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")

    # Primary link: h1 → s1
    net.addLink(h1, s1, intfName1="h1-eth0")
    # Secondary link: h1 → s3 (backup path)
    net.addLink(h1, s3, intfName1="h1-eth1")
    # Inter-switch links
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    # h2 connects to s3
    net.addLink(h2, s3)

    net.build()
    net.start()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Baseline: h1 → h2 via primary link (s1)\n")
    loss_baseline = net.ping([h1, h2])

    info("*** 2. Killing h1's primary link (h1-s1) — forcing migration to s3\n")
    net.configLinkStatus("h1", "s1", "down")
    time.sleep(2)

    info(
        "*** 3. Testing: h1 → h2 after migration (expected: FAIL due to immutable tracker)\n"
    )
    loss_after = net.ping([h1, h2])

    info("*** 4. Restoring primary link\n")
    net.configLinkStatus("h1", "s1", "up")
    time.sleep(3)

    info("*** 5. Verify recovery after returning to original position\n")
    loss_recovered = net.ping([h1, h2])

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # The controller is DESIGNED to fail here — immutability is intentional.
    # PASS = baseline works, migration fails (as designed), recovery works.
    # If loss_after == 0, the immutability rule was violated (BUG).
    designed_to_fail = (
        loss_baseline == 0.0
        and loss_after == 100.0  # Controller refuses to update h1's location
        and loss_recovered == 0.0  # Back to original position works
    )

    if designed_to_fail:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m   PASS (immobility correctly enforced)  \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m  FAIL (Baseline: {loss_baseline}%, AfterMove: {loss_after}%, "
            f"Recover: {loss_recovered}%) \033[0m"
        )
        if loss_after == 0.0:
            print(
                "\033[93m  NOTE: AfterMove=0% means immutability was violated!\033[0m"
            )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Host Relocation / Immutable Tracker Edge Case ---\n")
    test_host_relocation_immutable_tracker()
