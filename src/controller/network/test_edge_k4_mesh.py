import sys
import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_k4_mesh_storm_resilience():
    r"""
    Edge Case: K4 full mesh storm resilience.

    Tests if the spanning-tree algorithm can handle a highly connected
    graph (6 links for 4 switches) without creating broadcast storms.

    Topology: K4 full mesh
        h1 - s1 --- s2 - h2
             | \   / |
             |  \ /  |
             s3 --- s4
             |      |
            h3     h4

    Each switch has one directly attached host.

    Phases:
    1. K4 mesh ping-all (ST must prune 3 of 6 links).
    2. Slice graph (s1-s2 and s3-s4 down), forcing ST reconvergence.
    3. Re-test after massive ST reconvergence.
    """
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    net = Mininet(controller=RemoteController, switch=OVSSwitch, build=False)
    net.addController("c0", ip="127.0.0.1", port=6653)

    switches = [net.addSwitch(f"s{i}", dpid=f"{i:016x}") for i in range(1, 5)]
    hosts = [
        net.addHost(f"h{i}", ip=f"10.0.0.{i}", mac=f"00:00:00:00:00:0{i}")
        for i in range(1, 5)
    ]

    # Connect 1 host to 1 switch
    for h, s in zip(hosts, switches):
        net.addLink(h, s)

    # K4 Fully connected mesh (combinations)
    net.addLink("s1", "s2")
    net.addLink("s1", "s3")
    net.addLink("s1", "s4")
    net.addLink("s2", "s3")
    net.addLink("s2", "s4")
    net.addLink("s3", "s4")

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for discovery (Spanning tree must prune 3 links logically)\n")
    time.sleep(5)

    info("*** 1. K4 Mesh PingAll (If ST loops exist, ARP storms will kill this)\n")
    loss_full = net.pingAll()

    info(
        "*** 2. Slicing graph: taking down s1-s2 and s3-s4 (forcing drastically new ST)\n"
    )
    net.configLinkStatus("s1", "s2", "down")
    net.configLinkStatus("s3", "s4", "down")
    time.sleep(3)

    info("*** 3. Re-test pingAll after massive Spanning Tree reconvergence\n")
    loss_sliced = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = loss_full == 0.0 and loss_sliced == 0.0

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (Full Mesh: {loss_full}%, Sliced: {loss_sliced}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running K4 Mesh Storm & ST Edge Case ---\n")
    test_k4_mesh_storm_resilience()
