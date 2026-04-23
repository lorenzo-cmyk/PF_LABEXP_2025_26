import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_rapid_flap_race_conditions():
    """
    Edge Case: Race conditions and rapid state invalidation.
    We rapidly toggle a link UP and DOWN.
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
    net.addLink(h2, s3)

    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s1, s3)  # The backup link

    net.build()
    net.start()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** Baseline verification\n")
    loss_initial = net.pingAll()

    info("*** Commencing chaotic link flapping on s1-s2 (Up/Down rapidly)\n")
    for i in range(5):
        net.configLinkStatus("s1", "s2", "down")
        time.sleep(0.5)
        net.configLinkStatus("s1", "s2", "up")
        time.sleep(0.5)
        info(f"    Flap {i + 1}/5 done...\n")

    info("*** Waiting for dust to settle...\n")
    time.sleep(4)

    info("*** Post-flap verify: Is controller state corrupted?\n")
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
    info("\n--- Running Flapping / Race Condition Edge Case ---\n")
    test_rapid_flap_race_conditions()
