"""Upstream socket header/idle timeouts."""
from gateway.upstream import state as _state



import os
from gateway import settings as wb_settings
from gateway.common.log import log


def upstream_timeouts(accounts_dir=None):
    """(header, idle) seconds for the chat upstream socket.
    header covers connect/TLS/first byte; idle bounds each mid-stream read."""
    directory = accounts_dir if accounts_dir is not None else _state.ACCOUNTS_DIR
    try:
        return wb_settings.upstream_timeouts(directory)
    except Exception:
        return 120.0, 300.0


def _apply_stream_idle_timeout(response, seconds):
    """Switch the upstream socket to the mid-stream idle timeout.
    Best effort: an unknown socket shape keeps the header timeout."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return False
    if seconds <= 0:
        return False
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        sock = getattr(fp, "_sock", None)
    if sock is None:
        return False
    try:
        sock.settimeout(seconds)
        return True
    except Exception:
        return False
