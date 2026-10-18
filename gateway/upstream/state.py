"""Shared mutable upstream state.
Holds the values server.main() injects at startup. Read as attributes."""
import threading

import os

from gateway.common import paths

#: active AccountPool; None until server.main() installs one.
_POOL = None

#: usage.jsonl directory; server.main() overrides the resolved --usage-dir.
USAGE_DIR = (os.environ.get("WB_USAGE_DIR")
             or os.path.join(os.path.dirname(os.path.dirname(
                 os.path.dirname(os.path.abspath(__file__)))), "usage"))

#: accounts dir for settings lookups.
ACCOUNTS_DIR = paths.ACCOUNTS_DIR

#: coarse lock covering catalogue and pool reads.
_lock = threading.Lock()
