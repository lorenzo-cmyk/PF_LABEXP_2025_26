import sys
import time
import subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel, info


def test_link_restore_stale_path_cache():
    r"""
    Edge Case: Stale path cache after link recovery causes broken routing.

    The path computer caches shortest paths. When a link goes DOWN,
    handle_link_failure calls invalidate() to clear the cache. But when
    a link comes back UP (link_add_handler), the cache is NOT invalidated.

    This means a previously-cached suboptimal path persists even after
    a better path becomes available via the restored link. Worse, if the
    restored link enables connectivity through a switch that was isolated,
    the stale cache may return a path through a switch that is no longer
    reachable — causing packet loss.

    Topology (diamond):
                s2
              /    \
        h1—s1      s4—h2
              \    /
                s3

    Phases:
    1. Baseline — working via equal-cost paths.
    2. Kill the bottom path (s1-s3, s3-s4). Cache now has s1→s2→s4.
    3. Kill the top path too (s1-s2, s2-s4). h1/h2 isolated. Cache
       invalidated. s2 and s3 are now isolated switches.
    4. Restore s1-s3 (bottom) — connectivity restored via s1→s3→s4.
       BUT: the path for (h1,h2) was cached as [s1, s2, s4] before the
       second failure invalidated it. After step 3, cache is empty.
    5. Restore s2-s4 (partial top) — s2 is no longer isolated but s1-s2
       is still down. The cached path [s1,s2,s4] would be broken anyway
       (no s1-s2 link). A fresh computation should return [s1,s3,s4].
    6. Send traffic — should work via s1→s3→s4.

    The real test is whether the controller correctly reconnects and
    recomputes after recovering only partial connectivity.
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

    net.addLink(s1, s2)
    net.addLink(s2, s4)
    net.addLink(s1, s3)
    net.addLink(s3, s4)

    net.build()
    net.start()

    info("*** Pinging to learn hosts (may fail — teaches controller MAC/IP)\n")
    net.pingAll()

    info("*** Waiting for discovery\n")
    time.sleep(5)

    info("*** 1. Baseline\n")
    loss_bl = net.ping([h1, h2])

    info("*** 2. Kill BOTTOM path (s1-s3, s3-s4) — top carries traffic\n")
    net.configLinkStatus("s1", "s3", "down")
    net.configLinkStatus("s3", "s4", "down")
    time.sleep(3)
    loss_top_only = net.ping([h1, h2])

    info("*** 3. Kill TOP path too (s1-s2, s2-s4) — hosts isolated\n")
    net.configLinkStatus("s1", "s2", "down")
    net.configLinkStatus("s2", "s4", "down")
    time.sleep(3)
    loss_isolated = net.ping([h1, h2])

    info("*** 4. Restore s1-s3 only (bottom half) — should revive\n")
    net.configLinkStatus("s1", "s3", "up")
    time.sleep(5)  # LLDP discovery
    loss_bottom_back = net.ping([h1, h2])

    info("*** 5. Restore s3-s4 (complete bottom path) — full revival\n")
    net.configLinkStatus("s3", "s4", "up")
    time.sleep(5)
    loss_full_bottom = net.ping([h1, h2])

    info("*** 6. Restore FULL top path\n")
    net.configLinkStatus("s1", "s2", "up")
    net.configLinkStatus("s2", "s4", "up")
    time.sleep(5)
    loss_all_restored = net.pingAll()

    net.stop()
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    passed = (
        loss_bl == 0.0
        and loss_top_only == 0.0
        and loss_isolated == 100.0
        and loss_bottom_back == 100.0
        and loss_full_bottom == 0.0
        and loss_all_restored == 0.0
    )

    if passed:
        print("\n\033[92m=========================================\033[0m")
        print("\033[92m                 PASS                    \033[0m")
        print("\033[92m=========================================\033[0m\n")
    else:
        print("\n\033[91m=========================================\033[0m")
        print(
            f"\033[91m      FAIL (BL:{loss_bl}% TOP:{loss_top_only}% "
            f"ISOL:{loss_isolated}% BB:{loss_bottom_back}% "
            f"FB:{loss_full_bottom}% ALL:{loss_all_restored}%) \033[0m"
        )
        print("\033[91m=========================================\033[0m\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    setLogLevel("info")
    info("\n--- Running Link Restore Stale Path Cache Test ---\n")
    test_link_restore_stale_path_cache()
