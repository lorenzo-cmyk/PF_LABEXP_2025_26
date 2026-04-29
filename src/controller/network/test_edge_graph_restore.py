import sys
import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_graph_restoration_logic():
    """
    Edge Case: The "Better Path Returns" scenario.
    Topology: s1 to s2 (direct short path), and s1 to s3 to s2 (indirect long path).
    Ensures that when a link comes UP after a period of being DOWN, the TopologyGraph
    truly registers the new LLDP messages and can immediately utilize it if the backup
    path subsequently fails.
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
    net.addLink(h2, s2)

    # Direct
    net.addLink(s1, s2)
    # Indirect
    net.addLink(s1, s3)
    net.addLink(s3, s2)

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Verify shortest path routing\n")
    loss_init = net.pingAll()

    info("*** 2. Bring down primary direct path (s1-s2)\n")
    net.configLinkStatus("s1", "s2", "down")
    time.sleep(3)

    info("*** 3. Verify backup routing via s3 still works\n")
    loss_backup = net.pingAll()

    info("*** 4. RESTORE primary direct path (s1-s2)\n")
    net.configLinkStatus("s1", "s2", "up")
    info(
        "*** Waiting 5 seconds for LLDP discovery to fully register the recovered link...\n"
    )
    time.sleep(5)

    info("*** 5. KILL the backup path (s1-s3)\n")
    # If the TopologyManager failed to add s1-s2 back to the graph when it recovered,
    # the controller will incorrectly think the network is partitioned now!
    net.configLinkStatus("s1", "s3", "down")
    time.sleep(3)

    info("*** 6. Verify Traffic seamlessly resumed on the recovered direct path\n")
    loss_restored = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_init == 0.0 and loss_backup == 0.0 and loss_restored == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Init: {loss_init}%, Backup: {loss_backup}%, Restored: {loss_restored}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Graph Link Restoration Edge Case ---\n")
    test_graph_restoration_logic()
