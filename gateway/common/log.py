"""Shared logging sink; server.main() may install one, else stderr."""
import sys
import threading

_sink = None
_lock = threading.Lock()
# A sink may itself call log() (the scheduler used to install its own log as
# the process sink). Without this guard that re-enters the sink forever, so a
# nested call goes straight to the stream instead.
_in_sink = threading.local()


def set_logger(fn):
    """Install the process-wide log sink (server startup)."""
    global _sink
    _sink = fn


def log(msg, level=None, tag=None):
    """Write one diagnostic line; falls back to stderr before injection.
    A broken sink or closed stderr must never take a request down."""
    sink = _sink
    if sink is not None and not getattr(_in_sink, "active", False):
        _in_sink.active = True
        try:
            return sink(msg, level)
        except Exception:
            pass
        finally:
            _in_sink.active = False
    with _lock:
        try:
            stream = sys.stderr if level in ("WARN", "ERROR") else sys.stdout
            stream.write("[wb2api] %s\n" % msg)
            stream.flush()
        except Exception:
            pass
