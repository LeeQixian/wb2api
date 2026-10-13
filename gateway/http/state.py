"""Shared HTTP-layer state.

Populated by main() and by tests; read through this module so writes are
visible everywhere.
"""
import os
import sys
import threading
import time

MAX_PAYLOAD_BYTES = int(os.environ.get("WB_MAX_PAYLOAD_BYTES", 16 * 1024 * 1024))
MAX_CONCURRENT_CHAT = int(os.environ.get("WB_MAX_CONCURRENT_CHAT", 8))
CHAT_SLOT_WAIT_SECONDS = float(os.environ.get("WB_CHAT_SLOT_WAIT", 30))
POOL_REFRESH_SECONDS = 60.0

CURRENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_ACCOUNTS_DIR = os.environ.get("WB_ACCOUNTS_DIR") or \
    os.path.join(os.path.dirname(CURRENT_DIR), "accounts")
_DEFAULT_USAGE_DIR = os.environ.get("WB_USAGE_DIR") or \
    os.path.join(os.path.dirname(CURRENT_DIR), "usage")

ARGS = None
POOL = None
ACCOUNTS_DIR = _DEFAULT_ACCOUNTS_DIR
USAGE_DIR = _DEFAULT_USAGE_DIR

_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CHAT)
_pool_lock = threading.Lock()


def refresh_pool(force=False):
    """Rescan accounts/ so `wbcli login` adds apply without a restart."""
    global POOL
    if POOL is None:
        return None
    now = time.time()
    if not force and now - getattr(POOL, "loaded_at", 0.0) < POOL_REFRESH_SECONDS:
        return POOL
    with _pool_lock:
        if not force and now - getattr(POOL, "loaded_at", 0.0) < POOL_REFRESH_SECONDS:
            return POOL
        try:
            POOL.load()
        except Exception as exc:
            sys.stderr.write("[wb2api] pool reload failed: %s\n" % exc)
    return POOL
