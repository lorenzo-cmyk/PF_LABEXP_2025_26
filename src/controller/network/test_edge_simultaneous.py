import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_simultaneous_link_failures():
    """
    Edge Case: Concurrency stress testing.
    Topology: s1 connected to s2 via 3 independent parallel paths (s3, s4, s5).
    We take down two of those paths at the precise same moment to check if the
    controller's FaultHandler and PathComputer avoid race conditions when handling
    concurrent port-down and LLDP-timeout events.
    """
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")
    s4 = net.addSwitch("s4", dpid="0000000000000004")
    s5 = net.addSwitch("s5", dpid="0000000000000005")

    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")

    net.addLink(h1, s1)
    net.addLink(h2, s2)

    # Path A
    net.addLink("s1", "s3")
    net.addLink("s3", "s2")
    # Path B
    net.addLink("s1", "s4")
    net.addLink("s4", "s2")
    # Path C
    net.addLink("s1", "s5")
    net.addLink("s5", "s2")

    net.build()
    net.start()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Verify baseline connectivity\n")
    loss_init = net.pingAll()

    info("*** 2. Executing simultaneous link failures (Path A: s1-s3, Path B: s1-s4)\n")
    net.configLinkStatus("s1", "s3", "down")
    net.configLinkStatus("s1", "s4", "down")
    time.sleep(3)

    info("*** 3. Verify traffic perfectly shifted to Path C (s1-s5-s2)\n")
    loss_shifted = net.pingAll()

    info("*** 4. Kill Path C (isolated!)\n")
    net.configLinkStatus("s5", "s2", "down")
    time.sleep(3)
    loss_isolated = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_init == 0.0 and loss_shifted == 0.0 and loss_isolated == 100.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Initial: {loss_init}%, Shifted: {loss_shifted}%, Isolated: {loss_isolated}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Simultaneous Failures Edge Case ---\n")
    test_simultaneous_link_failures()
