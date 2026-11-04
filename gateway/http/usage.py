"""usage.jsonl row writing."""

import json
import os
import threading
import time

from gateway.common import paths
from gateway.common.log import log
from gateway.http import state as _state


_WRITE_LOCK = threading.Lock()


def _append_line(path, text):
    """Append one JSONL row atomically.

    A single os.write() on an O_APPEND descriptor is atomic on POSIX, so two
    request threads can never interleave halves of a row. The old text-mode
    `open(..., "a").write()` split one logical write into several syscalls
    under the buffer, and concurrent writers then silently lost rows (each
    lost row was also absent from every later fold, with no error raised)."""
    payload = text.encode("utf-8")
    with _WRITE_LOCK:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
        finally:
            os.close(fd)


def record_row(row):
    """Append one usage line. Errors must never break the served response."""
    try:
        os.makedirs(_state.USAGE_DIR, exist_ok=True)
        row = dict(row)
        row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        # epoch seconds: the daily-token guard folds usage.jsonl on `at`
        row["at"] = time.time()
        line = json.dumps(row, ensure_ascii=False) + "\n"
        _append_line(os.path.join(_state.USAGE_DIR, "usage.jsonl"), line)
    except Exception as exc:
        log("usage write failed: %s" % exc, "WARN")
        return
    # The balance refresher visits one account per tick, so subtract this
    # request's real cost now, or the reserve guard goes stale between ticks.
    amount = row.get("credit")
    uid = row.get("account")
    pool = _state.POOL
    if amount and uid and pool is not None:
        try:
            account = pool.get(uid)
            if account is not None:
                account.consume_credit(amount)
        except Exception:
            pass


def _usage_fields(usage):
    if not usage:
        return {}
    d = {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
    }
    comp = usage.get("completion_tokens_details") or {}
    prompt = usage.get("prompt_tokens_details") or {}
    if comp.get("reasoning_tokens"):
        d["reasoning_tokens"] = comp["reasoning_tokens"]
    if prompt.get("cached_tokens"):
        d["cached_tokens"] = prompt["cached_tokens"]
    for key in ("credit", "credits", "cost"):
        if usage.get(key) is not None:
            d["credit"] = usage[key]
            break
    return d


def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id")
        or headers.get("Conversation-Id")
        or headers.get("X-Session-Id")
        or headers.get("Session-Id")
        or payload.get("conversation_id")
        or payload.get("session_id")
        or (payload.get("metadata") or {}).get("conversation_id")
    )
    return str(key).strip() if key else None
