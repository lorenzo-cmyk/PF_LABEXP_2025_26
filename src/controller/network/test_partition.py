import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_partition():
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")
    s4 = net.addSwitch("s4", dpid="0000000000000004")

    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")
    h3 = net.addHost("h3", ip="10.0.0.3", mac="00:00:00:00:00:03")
    h4 = net.addHost("h4", ip="10.0.0.4", mac="00:00:00:00:00:04")

    net.addLink(h1, s1)
    net.addLink(s1, s2)
    net.addLink(s2, h2)
    net.addLink(h3, s3)
    net.addLink(s3, s4)
    net.addLink(s4, h4)
    net.addLink(s2, s3)  # Bridge

    net.build()
    net.start()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    net.pingAll()

    info("*** 2. Partitioning network (Bridge s2-s3 down)\n")
    net.configLinkStatus("s2", "s3", "down")
    time.sleep(3)

    info("*** 3. Test within Island 1 (h1 to h2) - Should SUCCEED\n")
    loss_i1 = net.ping([h1, h2])

    info("*** 4. Test within Island 2 (h3 to h4) - Should SUCCEED\n")
    loss_i2 = net.ping([h3, h4])

    info("*** 5. Test across Partition (h1 to h3) - Should FAIL gracefully\n")
    loss_cross = net.ping([h1, h3])

    info("*** 6. Restoring bridge link (s2-s3 up)\n")
    net.configLinkStatus("s2", "s3", "up")
    time.sleep(5)

    info("*** 7. Final verify - entire network\n")
    loss_final = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = (
        loss_i1 == 0.0 and loss_i2 == 0.0 and loss_cross == 100.0 and loss_final == 0.0
    )
    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (I1: {loss_i1}%, I2: {loss_i2}%, Cross: {loss_cross}%, Final: {loss_final}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Network Partition Test ---\n")
    test_partition()
