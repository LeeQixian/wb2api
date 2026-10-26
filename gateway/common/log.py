"""Shared logging sink; server.main() may install one, else stderr."""
import sys
import threading

_sink = None
_lock = threading.Lock()
# A sink may itself call log(); without this guard the call re-enters the sink
# forever, so a nested call goes straight to the stream instead.
_in_sink = threading.local()


def set_logger(fn):
    """Install the process-wide log sink (server startup)."""
    global _sink
    _sink = fn


def log(msg, level=None, tag=None):
    """Write one diagnostic line; without a sink it uses the local streams.
    A broken sink or closed stream must never take a request down."""
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
