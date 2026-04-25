import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_st_irrelevant_link():
    """
    Edge Case: Unrelated path fault isolation.

    Verifies that a fault on a link not used by the active flows does
    not disrupt existing connectivity.

    Topology:
        h1 - s1 - s2 - h2
             |
             s3 - s4

    Active flows between h1 and h2 use the direct s1-s2 path. The s3-s4
    link is irrelevant to this communication.

    Phases:
    1. Baseline ping (installs optimal flows via s1-s2).
    2. Take down irrelevant link s3-s4.
    3. Verify post-failure connectivity is unaffected.
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
    net.addLink(h2, s2)

    net.addLink(s1, s2)  # Primary
    net.addLink(s1, s3)
    net.addLink(s3, s4)  # The victim
    net.addLink(s4, s2)

    net.build()
    net.start()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Verify initial connectivity (installs optimal flows)\n")
    loss_initial = net.pingAll()

    info("*** 2. Taking down an irrelevant link (s3-s4)\n")
    net.configLinkStatus("s3", "s4", "down")
    time.sleep(3)

    info("*** 3. Verify post-failure connectivity (flows on s1-s2 should be intact)\n")
    loss_final = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_initial == 0.0 and loss_final == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Initial: {loss_initial}%, Final: {loss_final}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Irrelevant Link Edge Case ---\n")
    test_st_irrelevant_link()
