import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_switch_death_and_rebirth():
    """
    Edge Case: Full switch lifecycle — disconnect from controller, then rejoin.

    Topology: Linear  h1 — s1 — s2 — s3 — h2

    Unlike link failures (tested extensively), this tests what happens when
    a switch FULLY DISCONNECTS from the controller (OFP channel drops).
    The controller should see OFPStateChange → DEAD and:
      1. Purge all routes that traverse the dead switch
      2. Delete flows on surviving switches pointing to the dead switch
      3. Remove the switch from the topology graph
      4. Purge host entries learned on the dead switch
      5. Recompute spanning tree without the dead switch

    When the switch RECONNECTS, the controller must:
      1. Accept new SwitchFeatures and re-register the switch
      2. Re-initialize ports from dp.ports
      3. Allow LLDP to re-discover links
      4. Recompute spanning tree with the returned switch
      5. Successfully forward traffic again

    This is the most destructive single-point failure possible — it
    exercises the entire cleanup + re-initialization pipeline.
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

    info("*** 1. Baseline pingAll\n")
    loss_baseline = net.pingAll()

    info("*** 2. DISCONNECTING s2 from controller (OFP channel drop)\n")
    # Remove the OpenFlow controller from s2 — simulates switch crash or
    # network partition between switch and controller.
    s2.cmd("ovs-vsctl del-controller s2")
    time.sleep(3)  # Wait for DEAD state + cleanup

    info("*** 3. Testing with s2 dead (h1 and h2 should be unreachable)\n")
    loss_partitioned = net.ping([h1, h2])

    info("*** 4. RECONNECTING s2 to controller\n")
    s2.cmd("ovs-vsctl set-controller s2 tcp:127.0.0.1:6653")
    time.sleep(7)  # Wait for reconnect + SwitchFeatures + LLDP discovery

    info("*** 5. Testing after s2 rejoins (should recover)\n")
    loss_rejoined = net.pingAll()

    info("*** 6. Second ping to confirm stable flows\n")
    loss_stable = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = (
        loss_baseline == 0.0
        and loss_partitioned == 100.0  # s2 was the only bridge
        and loss_rejoined == 0.0
        and loss_stable == 0.0
    )

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m      PASS (switch rebirth successful)   \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m  FAIL (Baseline: {loss_baseline}%, Dead: {loss_partitioned}%, "
            f"Rejoin: {loss_rejoined}%, Stable: {loss_stable}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Switch Death & Rebirth Edge Case ---\n")
    test_switch_death_and_rebirth()
