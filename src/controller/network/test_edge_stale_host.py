import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_stale_host_cleanup_on_link_add():
    """
    Edge Case: Startup race — packets arrive before LLDP discovers links.

    Topology: Ring h1 — s1 — s2 — s3 — h2 with extra link s1—s3.

        h1 — s1 — s2 — s3 — h2
              └──────────┘

    The most subtle race in the controller: when switches first connect,
    all ports are assumed edge (host-facing). LLDP hasn't discovered
    inter-switch links yet, so the spanning tree has zero tree edges.
    Broadcast/multicast/unknown packets flood on EVERY port including
    inter-switch links → broadcast storm.

    During this storm, the host tracker sees h1's source MAC arriving on
    s2's and s3's internal ports (the flooded packets loop through).
    It records h1 at the wrong location (e.g., s2 port-3 instead of s1
    port-1).

    The fix: on EventLinkAdd, the controller calls remove_by_port() on
    both ends of the new link, purging any hosts wrongly learned on what
    are now known to be internal ports. Those hosts get re-learned on
    correct edge ports.

    This test races pings against LLDP discovery. We don't wait for
    discovery — we ping immediately after switches connect. The first
    ping may fail (stale locations), but after LLDP finishes and the
    cleanup runs, the second ping MUST succeed.

    If the cleanup is broken, the second ping also fails (host tracker
    permanently corrupted with wrong locations).
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
    net.addLink(s1, s3)  # Ring closure

    net.build()
    net.start()

    # DELIBERATELY SHORT WAIT — race against LLDP discovery.
    # 1 second is enough for switches to connect and ports to initialize,
    # but LLDP link discovery may still be in progress.
    info("*** Waiting 1s (racing against LLDP)\n")
    time.sleep(1)

    info("*** 1. Early ping (may fail — LLDP not done, host tracker may be stale)\n")
    loss_early = net.ping([h1, h2])

    # Now wait for LLDP to fully discover all links and the cleanup to run
    info("*** 2. Waiting for LLDP discovery to complete (5s)\n")
    time.sleep(5)

    info("*** 3. Post-discovery ping (MUST succeed if stale cleanup works)\n")
    loss_after = net.ping([h1, h2])

    info("*** 4. Second confirmation ping (flows should be stable now)\n")
    loss_stable = net.ping([h1, h2])

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # The early ping may or may not work (depends on timing).
    # The post-discovery ping MUST work — if it doesn't, stale host cleanup failed.
    passed = loss_after == 0.0 and loss_stable == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Early: {loss_early}%, After: {loss_after}%, "
            f"Stable: {loss_stable}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Stale Host Cleanup / Startup Race Edge Case ---\n")
    test_stale_host_cleanup_on_link_add()
