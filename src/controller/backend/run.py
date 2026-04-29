#!/usr/bin/env python
"""Launcher for the SDN controller.

Must be run directly (not imported).  eventlet.monkey_patch() is called
after os-ken imports; if os-ken or its dependencies touch stdlib
networking/threading before patching, subtle runtime issues may arise.

Usage:
    python run.py
"""

from os_ken.base.app_manager import AppManager
from os_ken.topology import switches as _  # noqa: F401 — registers cfg opts
from os_ken import cfg

import os
import warnings
import logging

# Use eventlet hub — we already monkey-patch with eventlet.
# The default 'native' hub uses threading.Thread which lacks .kill(),
# causing AppManager.run_apps() to crash on cleanup.
os.environ.setdefault("OSKEN_HUB_TYPE", "eventlet")

import eventlet

eventlet.monkey_patch()

# Suppress eventlet's own deprecation noise and the harmless RLock warning
warnings.filterwarnings("ignore")

# Enable LLDP link discovery in os-ken's Switches app.
# observe-links defaults to False, which means LLDP is NEVER sent.
# Import switches first to register its cfg options, then override.
cfg.CONF.set_override("observe_links", True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Silence noisy third-party loggers
logging.getLogger("os_ken.base.app_manager").setLevel(logging.INFO)
logging.getLogger("os_ken.ofproto").setLevel(logging.WARNING)
logging.getLogger("os_ken.lib").setLevel(logging.WARNING)
logging.getLogger("os_ken.controller").setLevel(logging.INFO)

if __name__ == "__main__":
    LOG = logging.getLogger("run")
    LOG.info("Starting SDN controller (os-ken + eventlet, LLDP enabled)")
    AppManager.run_apps(
        [
            "os_ken.topology.switches",  # LLDP-based topology discovery
            "backend",
        ]
    )
