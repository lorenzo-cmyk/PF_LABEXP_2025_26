import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_multi_failure():
    """
    Creates a full mesh with 3 switches and 3 hosts.
    Progressively disables links to observe routing behavior.
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

    net.addLink(h1, s1)
    net.addLink(h2, s2)
    net.addLink(h3, s3)

    # Full mesh
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s1, s3)

    net.build()
    net.start()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Baseline pingAll (should SUCCEED)\n")
    loss_initial = net.pingAll()

    info("*** 2. First failure: s1-s2 down (Traffic s1->s2 should route via s3)\n")
    net.configLinkStatus("s1", "s2", "down")
    time.sleep(3)
    loss_first_fail = net.pingAll()

    info("*** 3. Second failure: s1-s3 down (s1/h1 is now completely isolated!)\n")
    net.configLinkStatus("s1", "s3", "down")
    time.sleep(3)

    info("*** 4. Testing isolated h1 to h2 (should FAIL)\n")
    loss_isolated = net.ping([h1, h2])

    info("*** 5. Testing active remaining fragment h2 to h3 (should SUCCEED)\n")
    loss_frag = net.ping([h2, h3])

    info("*** 6. Restoring s1-s2\n")
    net.configLinkStatus("s1", "s2", "up")
    time.sleep(4)

    info("*** 7. Final verify (should SUCCEED)\n")
    loss_final = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = (
        loss_initial == 0.0
        and loss_first_fail == 0.0
        and loss_isolated == 100.0
        and loss_frag == 0.0
        and loss_final == 0.0
    )

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Initial: {loss_initial}%, Fail1: {loss_first_fail}%, Iso: {loss_isolated}%, Frag: {loss_frag}%, Final: {loss_final}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Multiple Link Failure Test ---\n")
    test_multi_failure()
