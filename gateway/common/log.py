"""Shared logging sink.

Every module logs through here. server.main() may install a custom sink;
without one, lines go to stderr.
"""
import sys
import threading

_sink = None
_lock = threading.Lock()


def set_logger(fn):
    """Install the process-wide log sink (server startup)."""
    global _sink
    _sink = fn


def log(msg, level=None, tag=None):
    """Write one diagnostic line; falls back to stderr before injection."""
    if _sink is not None:
        return _sink(msg, level)
    with _lock:
        stream = sys.stderr if level in ("WARN", "ERROR") else sys.stdout
        stream.write("[wb2api] %s\n" % msg)
        stream.flush()
