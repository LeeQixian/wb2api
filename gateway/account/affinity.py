"""Session-to-account affinity binding."""

import threading
import time


def _human_delta(seconds):
    if seconds is None:
        return None
    if seconds <= 0:
        return "expired"
    days = seconds / 86400.0
    if days >= 1:
        return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1:
        return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)


def _human_left(seconds):
    """Cooldown remainder in seconds: 42s / 1m30s / 2h30m.
    `_human_delta` rounds a minute down to "0 min", which reads as already free."""
    secs = max(0, int(round(seconds)))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        m, s = divmod(secs, 60)
        return "%dm%02ds" % (m, s) if s else "%dm" % m
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return "%dh%02dm" % (h, m) if m else "%dh" % h


class SessionAffinity(object):
    def __init__(self, ttl=7200, max_entries=5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self.bindings = {}
        self._lock = threading.Lock()

    def get(self, key):
        if not key:
            return None
        with self._lock:
            entry = self.bindings.get(key)
            if not entry:
                return None
            uid, exp = entry
            if time.time() > exp:
                self.bindings.pop(key, None)
                return None
            self.bindings[key] = (uid, time.time() + self.ttl)
            return uid

    def bind(self, key, uid):
        if not key or not uid:
            return
        with self._lock:
            if len(self.bindings) >= self.max_entries:
                now = time.time()
                self.bindings = {k: v for k, v in self.bindings.items() if v[1] > now}
            self.bindings[key] = (uid, time.time() + self.ttl)

    def unbind(self, key):
        if not key:
            return
        with self._lock:
            self.bindings.pop(key, None)
