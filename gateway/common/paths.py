"""Filesystem paths resolved at runtime by server.main().

Modules read these instead of holding their own copies.
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ACCOUNTS_DIR = os.environ.get("WB_ACCOUNTS_DIR") or os.path.join(_ROOT, "accounts")
USAGE_DIR = os.environ.get("WB_USAGE_DIR") or os.path.join(_ROOT, "usage")
