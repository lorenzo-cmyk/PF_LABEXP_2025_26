import sys
import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_equal_cost_flapping():
    r"""
    Edge Case: Equal-cost tie-breaking and symmetry during constant failure.

    Tests that traffic survives repeated path transitions between two
    equal-cost paths in a diamond topology.

    Topology: Diamond
           s2
         /    \
    h1 -s1    s4- h2
         \    /
           s3

    Cost is identical via s2 or s3.

    Phases:
    1. Baseline ping (both paths active).
    2. Tear down top path (s1-s2).
    3. Restore top, tear down bottom path (s1-s3).
    4. Tear down both remaining links (s2-s4, s1-s3) -- creates partition (100% loss).
    5. Restore all links.
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

    net.addLink(h1, s1)
    net.addLink(h2, s4)

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

    loss_initial = net.pingAll()

    info("*** Tearing down TOP path (s1-s2)\n")
    net.configLinkStatus("s1", "s2", "down")
    time.sleep(2)
    loss_top_down = net.pingAll()

    info("*** Bringing TOP back up, tearing down BOTTOM (s1-s3)\n")
    net.configLinkStatus("s1", "s2", "up")
    net.configLinkStatus("s1", "s3", "down")
    time.sleep(2)
    loss_bot_down = net.pingAll()

    info("*** Tearing down BOTH (s2-s4 and s1-s3) -> Creating absolute partition\n")
    net.configLinkStatus("s2", "s4", "down")
    time.sleep(2)
    loss_part = net.ping([h1, h2])

    info("*** Restoring all\n")
    net.configLinkStatus("s2", "s4", "up")
    net.configLinkStatus("s1", "s3", "up")
    time.sleep(5)
    loss_final = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = (
        loss_initial == 0.0
        and loss_top_down == 0.0
        and loss_bot_down == 0.0
        and loss_part == 100.0
        and loss_final == 0.0
    )

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Initial: {loss_initial}%, TopDown: {loss_top_down}%, BotDown: {loss_bot_down}%, Part: {loss_part}%, Final: {loss_final}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Equal Cost & Symmetry Edge Case ---\n")
    test_equal_cost_flapping()
