"""Backoff, breaker and park-until helpers for per-account failure governance."""

import time

SOFT_RATE_BASE = 600.0
SOFT_RATE_MAX = 7200.0
SOFT_RATE_UNVERIFIED_BASE = 60.0
SOFT_RATE_UNVERIFIED_MAX = 600.0
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = 1800.0
BREAKER_COOLDOWN_MAX = 21600.0
DEGRADE_THRESHOLD = 5
DEGRADE_COOLDOWN = 600.0
DEGRADE_COOLDOWN_MAX = 7200.0


def _exponential_backoff(count, base, cap, offset):
    """base * 2 ** (count - offset), clamped to cap and to 20 steps."""
    step = max(0, int(count) - int(offset))
    return min(float(base) * (2 ** min(step, 20)), float(cap))


def soft_backoff(streak):
    """Cooldown seconds for `streak` consecutive account-level soft limits."""
    return _exponential_backoff(streak, SOFT_RATE_BASE, SOFT_RATE_MAX, 1)


def breaker_backoff(fails):
    """Breaker cooldown for `fails` consecutive hard failures."""
    return _exponential_backoff(fails, BREAKER_COOLDOWN, BREAKER_COOLDOWN_MAX,
                                BREAKER_THRESHOLD)


def degrade_backoff(fails):
    """Degrade window for `fails` consecutive unknown failures."""
    return _exponential_backoff(fails, DEGRADE_COOLDOWN, DEGRADE_COOLDOWN_MAX,
                                DEGRADE_THRESHOLD)


def unverified_soft_backoff(streak):
    """Window for `streak` consecutive unscoped soft-limit 429s (hub 72e51b4)."""
    return _exponential_backoff(streak, SOFT_RATE_UNVERIFIED_BASE,
                                SOFT_RATE_UNVERIFIED_MAX, 1)


def next_local_4am(now=None):
    """Epoch of the next local 04:00.

    The daily reset that puts CN credits back happens overnight, so the
    04:00 wall is what an out-of-credits park waits for. A timestamp already
    past 04:00 rolls to tomorrow, so a deadline is never reused in place.
    """
    now = time.time() if now is None else now
    lt = time.localtime(now)
    stamp = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                         4, 0, 0, 0, 0, -1))
    if stamp <= now:
        stamp += 86400
    return stamp
