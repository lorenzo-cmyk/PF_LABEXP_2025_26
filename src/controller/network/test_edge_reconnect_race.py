import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_reconnect_race_recovery():
    """
    Edge Case: Switch disconnect/reconnect race condition.

    When a switch disconnects and reconnects rapidly, os-ken creates a new
    Datapath for the new connection but the old Datapath fires a DEAD state
    change event. If the controller processes the stale DEAD event after the
    new connection is already registered, it tears down the freshly-connected
    switch — unregistering its datapath, purging routes, removing it from
    the topology graph.

    This test runs the disconnect/reconnect cycle 5 times in rapid succession
    (3s between cycles) to stress the race window. Each cycle verifies that
    connectivity recovers after the reconnect.

    Topology: Linear  h1 -- s1 -- s2 -- s3 -- h2
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

    net.build()
    net.start()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** Baseline\n")
    loss_bl = net.pingAll()

    CYCLES = 5
    all_recoveries_ok = True

    for i in range(1, CYCLES + 1):
        info(f"\n*** Cycle {i}/{CYCLES}: Disconnecting s2\n")
        s2.cmd("ovs-vsctl del-controller s2")
        time.sleep(2)

        info(f"*** Cycle {i}/{CYCLES}: Ping during dead phase (should be 100%% loss)\n")
        loss_dead = net.ping([h1, h2])
        if loss_dead != 100.0:
            info(f"    WARNING: dead ping = {loss_dead}% (expected 100%)\n")

        info(f"*** Cycle {i}/{CYCLES}: Reconnecting s2\n")
        s2.cmd("ovs-vsctl set-controller s2 tcp:127.0.0.1:6653")
        time.sleep(6)

        info(f"*** Cycle {i}/{CYCLES}: Verify recovery\n")
        loss_recovery = net.pingAll()
        if loss_recovery != 0.0:
            info(f"    FAIL: recovery ping = {loss_recovery}% loss\n")
            all_recoveries_ok = False

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_bl == 0.0 and all_recoveries_ok

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Baseline: {loss_bl}%, "
            f"AllRecoveriesOK: {all_recoveries_ok}) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Reconnect Race Recovery Test ---\n")
    test_reconnect_race_recovery()
