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
_daily_usage = {"day": "", "totals": None, "offset": 0, "at": 0.0}
_daily_usage_lock = threading.Lock()
_STATS_TTL = float(os.environ.get("WB_STATS_TTL", 15))


def usage_log_path():
    return os.path.join(_state.USAGE_DIR, "usage.jsonl")


def row_outcome(row):
    """Terminal state of a request row (the hub's analytics helper)."""
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
        # the midnight filter drops whatever old rows are still unread.
        totals = dict(c["totals"] or {}) if c["day"] == day else {}
        offset = int(c["offset"] or 0)
        try:
            size = os.path.getsize(usage_log_path())
        except OSError:
            size = 0
        if offset > size:
            totals, offset = {}, 0
        try:
            totals, offset = _scan_daily_tokens(offset, totals)
        except Exception as exc:
            log("daily token scan failed: %s" % exc)
            _daily_usage.update({"day": day, "totals": None, "offset": 0,
                                 "at": time.time()})
            return None
        _daily_usage.update({"day": day, "totals": totals, "offset": offset,
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
