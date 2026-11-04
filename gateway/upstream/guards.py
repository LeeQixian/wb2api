"""Daily token guard and usage-log folding."""
from gateway.upstream import state as _state



import json
import os
import threading
import time
from gateway import settings as wb_settings
from gateway.common.log import log
from gateway.common import paths


from gateway.upstream import state as _state
_daily_usage = {"day": "", "totals": None, "offset": 0, "at": 0.0,
                # (size, dev, inode) of the log the cached offset was taken
                # against, plus the 64 bytes just before it. Without these a
                # rewritten-then-regrown log (copytruncate) looks like a plain
                # append and the fold both keeps deleted rows and skips the
                # new file's first `offset` bytes.
                "key": None, "tail": None}
_daily_usage_lock = threading.Lock()
_STATS_TTL = float(os.environ.get("WB_STATS_TTL", 15))


def usage_log_path():
    return os.path.join(_state.USAGE_DIR, "usage.jsonl")


# How many bytes of the already-counted prefix are read back on every refresh
# to prove that prefix still holds the same bytes.
_TAIL_SIGNATURE = 64


def _usage_log_key():
    """(size, device, inode) of the usage log, or a zero key when it is gone."""
    try:
        info = os.stat(usage_log_path())
        return (info.st_size, info.st_dev, info.st_ino)
    except OSError:
        return (0, 0, 0)


def _log_offset_holds(previous, current, offset):
    """True when `offset` still sits inside the file `previous` described."""
    return (previous is not None
            and previous[1] == current[1] and previous[2] == current[2]
            and current[0] >= offset)


def _log_tail_signature(offset):
    """The last bytes before `offset`, or None when they cannot be read."""
    if offset <= 0:
        return b""
    try:
        with open(usage_log_path(), "rb") as fh:
            start = max(0, offset - _TAIL_SIGNATURE)
            fh.seek(start)
            return fh.read(offset - start)
    except OSError:
        return None


def row_outcome(row):
    """Terminal state of a request row."""
    o = row.get("outcome")
    if o:
        return o
    return "failed" if row.get("error") else "completed"


def _local_midnight(ts=None, days_back=0):
    """Local midnight `days_back` days before `ts` (default: now).
    mktime normalises out-of-range days instead of raising."""
    lt = time.localtime(ts if ts is not None else time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - days_back, 0, 0, 0, 0, 0, -1))


def _scan_daily_tokens(offset, totals):
    """Fold rows at/after today's local midnight into `totals`.
    Returns (totals, new_offset); a line without its newline waits for next scan."""
    midnight = _local_midnight()
    with open(usage_log_path(), encoding="utf-8") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                break
            if not line.endswith("\n"):
                return totals, pos
            offset = fh.tell()
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if (row.get("at") or 0) < midnight:
                continue
            # Same rule as the analytics scan: a client cancellation is not a
            # consumed request, and its token counts are incomplete.
            if row_outcome(row) == "client_aborted":
                continue
            uid = row.get("account")
            if not uid:
                continue
            totals[uid] = totals.get(uid, 0) + (row.get("total_tokens") or 0)
    return totals, offset


def daily_tokens_by_account(ttl=None):
    """uid -> tokens since local midnight, cached for `ttl` seconds.
    None means the log was unreadable; callers keep that distinct from zero."""
    ttl = _STATS_TTL if ttl is None else ttl
    day = time.strftime("%Y-%m-%d")
    now = time.time()
    with _daily_usage_lock:
        c = _daily_usage
        if c["day"] == day and c["totals"] is not None and (now - c["at"]) < ttl:
            return dict(c["totals"])
        # A new day keeps the byte offset: everything past it is today's, and
        # the midnight filter drops any unread row from an earlier day.
        totals = dict(c["totals"] or {}) if c["day"] == day else {}
        offset = int(c["offset"] or 0)
        log_key = _usage_log_key()
        # Resume the cached fold only when the bytes it counted are provably
        # still there. `offset > size` alone misses the case that actually
        # bites: logrotate's copytruncate empties the file in place (same
        # inode) and it then grows past the old offset, so the size test
        # passes while the bytes underneath were replaced. The daily counters
        # would then keep yesterday's numbers *and* silently skip every row in
        # the first `offset` bytes of today's file. The key covers
        # size/device/inode; re-reading the seam is what tells an append apart
        # from a rewrite.
        if offset and not (c.get("key") is not None
                           and c.get("tail") is not None
                           and _log_offset_holds(c["key"], log_key, offset)
                           and _log_tail_signature(offset) == c["tail"]):
            totals, offset = {}, 0
        try:
            totals, offset = _scan_daily_tokens(offset, totals)
        except Exception as exc:
            log("daily token scan failed: %s" % exc)
            _daily_usage.update({"day": day, "totals": None, "offset": 0,
                                 "key": None, "tail": None, "at": time.time()})
            return None
        # Record the position consumed *and* the seam behind it, so the next
        # call can prove this prefix still holds the same bytes.
        _daily_usage.update({"day": day, "totals": totals, "offset": offset,
                             "key": _usage_log_key(),
                             "tail": _log_tail_signature(offset),
                             "at": time.time()})
        return dict(totals)


def seconds_until_local_midnight():
    """Seconds until the local day rolls over (at least a minute)."""
    lt = time.localtime()
    nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return max(60, int(nxt - time.time()))


def apply_daily_token_limit(pool=None):
    """Push the daily token setting and today's counts into the pool.
    A pool without the guard API (test doubles) is a no-op."""
    pool = pool if pool is not None else _state._POOL
    directory = getattr(pool, "dir", None)
    if pool is None or directory is None or not hasattr(pool, "apply_daily_token_limit"):
        return 0
    limit = wb_settings.daily_token_limit(directory)
    usage = daily_tokens_by_account() if limit > 0 else None
    return pool.apply_daily_token_limit(limit, usage)


def auto_switch_product_enabled(pool=None):
    """Whether a 429 may rotate the outbound identity (settings toggle)."""
    pool = pool if pool is not None else _state._POOL
    if pool is None:
        return False
    try:
        return wb_settings.auto_switch_product(pool.dir)
    except Exception:
        return False
