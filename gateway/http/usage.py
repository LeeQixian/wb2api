"""usage.jsonl row writing."""

import json
import os
import threading
import time

from gateway.common import paths
from gateway.common.log import log
from gateway.http import state as _state


def record_row(row):
    """Append one usage line. Errors must never break the served response."""
    try:
        os.makedirs(_state.USAGE_DIR, exist_ok=True)
        row = dict(row)
        row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        # epoch seconds: the daily-token guard folds usage.jsonl on `at`
        row["at"] = time.time()
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with open(os.path.join(_state.USAGE_DIR, "usage.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(line)
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
