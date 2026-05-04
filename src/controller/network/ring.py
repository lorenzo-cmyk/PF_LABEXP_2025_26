"""Mininet ring topology: h1 — s1 — s2 — s3 — h2 with extra link s1—s3

Usage:
    sudo python ring.py

Starts Mininet with a remote os-ken controller on localhost:6653.
The controller uses proxy-ARP and zero-trust broadcast drop so ring
topologies are safe without spanning-tree.
"""

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def ring_topo() -> None:
    net = Mininet(
        controller=RemoteController,
        switch=OVSSwitch,
        build=False,
    )

    info("*** Adding controller\n")
    net.addController("c0", ip="127.0.0.1", port=6653)

    info("*** Adding switches\n")
    s1 = net.addSwitch("s1", dpid="0000000000000001")
    s2 = net.addSwitch("s2", dpid="0000000000000002")
    s3 = net.addSwitch("s3", dpid="0000000000000003")

    info("*** Adding hosts\n")
    h1 = net.addHost("h1", ip="10.0.0.1", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2", mac="00:00:00:00:00:02")

    info("*** Adding links (ring: s1—s2—s3—s1)\n")
    net.addLink(h1, s1)
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(s3, h2)
    # Extra link to create the ring
    net.addLink(s1, s3)

    info("*** Starting network\n")
    net.build()
    net.start()

    info("*** Running CLI\n")
    CLI(net)

    info("*** Stopping network\n")
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    ring_topo()
