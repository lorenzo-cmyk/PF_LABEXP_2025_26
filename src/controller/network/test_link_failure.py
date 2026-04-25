import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_single_failure_recovery():
    """
    Single link failure and recovery in a ring topology.

    Verifies that traffic reroutes through an alternate path when the
    primary link fails, and returns to normal when the link is restored.

    Topology: Ring
        h1 - s1 - s2
              \   /
               s3
               |
              h2

    Phases:
    1. Baseline full connectivity.
    2. Simulate failure on link s1-s2.
    3. Verify connectivity via alternate path s1-s3.
    4. Restore link s1-s2.
    5. Final connectivity verification.
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
    net.addLink(s3, h2)

    # Ring
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s1, s3)

    net.build()
    net.start()

    info("*** Waiting for topology discovery\n")
    time.sleep(5)

    info("*** 1. Initial full connectivity test\n")
    loss_initial = net.pingAll()

    info("*** 2. Simulating failure on link s1-s2\n")
    net.configLinkStatus("s1", "s2", "down")
    time.sleep(3)

    info("*** 3. Testing connectivity after failure (Should SUCCEED via s1-s3)\n")
    loss1 = net.pingAll()

    info("*** 4. Restoring link s1-s2\n")
    net.configLinkStatus("s1", "s2", "up")
    time.sleep(3)

    info("*** 5. Final connectivity test\n")
    loss2 = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_initial == 0.0 and loss1 == 0.0 and loss2 == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Initial: {loss_initial}%, Loss1: {loss1}%, Loss2: {loss2}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Link Failure Recovery Test ---\n")
    test_single_failure_recovery()
