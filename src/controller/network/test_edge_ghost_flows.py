import sys
import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_ghost_flow_accumulation():
    r"""
    Edge Case: Stale flow accumulation after repeated fail-recover cycles.

    Topology: Diamond with two equal-cost paths.
           s2
         /    \
    h1 -s1    s4- h2
         \    /
           s3

    We cycle the top path (s1-s2 and s2-s4) down and up 6 times, pinging
    after each transition. If the controller's flow deletion is incomplete
    (wrong match, missing priority, stale cookie), old flow entries will
    accumulate in the switches' flow tables. Eventually a stale entry with
    a higher match priority could intercept traffic and forward it to a
    dead port, causing packet loss.

    This is the kind of bug that passes on the first 2-3 iterations but
    fails on iteration 5+ as the flow table fills with ghost entries.
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

    # Top path
    net.addLink(s1, s2)
    net.addLink(s2, s4)
    # Bottom path
    net.addLink(s1, s3)
    net.addLink(s3, s4)

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Baseline ping\n")
    loss_baseline = net.pingAll()

    CYCLES = 6
    all_passed = True
    for i in range(1, CYCLES + 1):
        info(f"\n*** Cycle {i}/{CYCLES}: Killing top path (s1-s2, s2-s4)\n")
        net.configLinkStatus("s1", "s2", "down")
        net.configLinkStatus("s2", "s4", "down")
        time.sleep(2)

        loss_down = net.ping([h1, h2])
        if loss_down > 0:
            info(f"    FAIL: ping after top-path down = {loss_down}% loss\n")
            all_passed = False

        info(f"*** Cycle {i}/{CYCLES}: Restoring top path\n")
        net.configLinkStatus("s1", "s2", "up")
        net.configLinkStatus("s2", "s4", "up")
        time.sleep(4)  # Allow LLDP re-discovery

        loss_up = net.ping([h1, h2])
        if loss_up > 0:
            info(f"    FAIL: ping after restore = {loss_up}% loss (ghost flows?)\n")
            all_passed = False

    info("\n*** Final verification\n")
    loss_final = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_baseline == 0.0 and loss_final == 0.0 and all_passed

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Baseline: {loss_baseline}%, Final: {loss_final}%, "
            f"AllCyclesOK: {all_passed}) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Ghost Flow Accumulation Edge Case ---\n")
    test_ghost_flow_accumulation()
