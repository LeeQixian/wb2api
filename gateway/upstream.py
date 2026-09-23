"""Upstream call layer: realm routing, account rotation, throttle handling.

Ported from workbuddy2api-hub/wb_proxy.py. This is where the upstream's
failure semantics are decoded and re-exposed as OpenAI-shaped errors:

  429 / code 6004  -> RateLimited with the model-scoped cooldown reset time
  403 / code 11140 -> ContentRejected, passed through WITHOUT cooling the pool
  401              -> rotate account (credential dead, model not blamed)
  5xx / TLS reset  -> transient: brief backoff, no cooldown, no rotation cost

open_upstream() logic is preserved line-for-line from the source; the POOL
global became a parameter and log() an injected callback.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from gateway import accounts as wb_accounts
from gateway import catalog as wb_catalog
from gateway import translate as tr
from gateway.translate import (  # noqa: F401  (re-exported for server/tests)
    detect_model_realm,
    exclusive_realm,
    is_chat_model,
    is_model_banned,
    banned_model_message,
    build_upstream_body,
    derive_affinity_key,
    prompt_cache_key_enabled,
    inject_prompt_cache_key,
)

UPSTREAM = "https://www.workbuddy.ai"
CHAT_PATH = "/v2/chat/completions"
MODELS_PATH = "/v2/enterprises/personal/models"
AFFINITY_DEBUG = os.environ.get("WB_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")
#: 429 时是否自动切换 cli <-> workbuddy 身分（两条独立配额线）。默认关：
#: 低频自用不需要，切换会多消耗上游请求。
AUTO_SWITCH_PRODUCT = os.environ.get("WB_AUTO_SWITCH_PRODUCT",
                                     "0").lower() in ("1", "true", "yes", "on")
MAX_PRODUCT_SWITCHES = 4

_lock = threading.Lock()


def log(msg, level=None, tag=None):
    sys.stderr.write("[wb2api] %s\n" % msg)


def set_logger(fn):
    """Install the server's logger for this module."""
    global log
    log = fn


# --------------------------------------------------------------------------
# realm switch (was the panel's global toggle; now POST /realm)
# --------------------------------------------------------------------------
def realm_state_file(accounts_dir):
    """Path of the persisted realm switch.

    Computed on every access rather than cached in a module constant: the
    source built the constant from the default ACCOUNTS_DIR at import time,
    so a later --accounts-dir still read and wrote the switch next to the
    script, and the reported exit did not match the configured account store.
    """
    return os.path.join(accounts_dir, "active_realm.json")


def get_realm():
    return tr.CURRENT_REALM


def set_realm(realm):
    """Single source of truth lives in translate.CURRENT_REALM (read by
    model_default_effort & friends); mirror it here for direct importers."""
    if realm in ("intl", "cn"):
        tr.CURRENT_REALM = realm
    return tr.CURRENT_REALM


def load_persisted_realm(accounts_dir):
    path = realm_state_file(accounts_dir)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            r = d.get("realm")
            if r in ("intl", "cn"):
                return set_realm(r)
        except Exception as e:
            log("could not load active realm: %s" % e)
    return get_realm()


def save_persisted_realm(accounts_dir, realm):
    if realm in ("intl", "cn"):
        set_realm(realm)
        try:
            os.makedirs(accounts_dir, exist_ok=True)
            with open(realm_state_file(accounts_dir), "w", encoding="utf-8") as fh:
                json.dump({"realm": realm, "updated_at": time.time(),
                           "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")},
                          fh, indent=2)
            log("persisted active realm '%s' to disk" % realm)
        except Exception as exc:
            log("failed to persist active realm: %s" % exc)
    return get_realm()


# --------------------------------------------------------------------------
# model catalog. The desktop-app config cache (read_product_config_models)
# is dropped: no client install on this box. Built-in catalog + live endpoint
# discovery remain.
# --------------------------------------------------------------------------
CN_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-pro",
]
INTL_UI_ORDER = [
    "deepseek-v4.1-flash",
    "gpt-6-astra",
    "hy4-preview-f",
    "hy4-preview",
    "hy3",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gemini-3.5-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
]


def merge_catalog(primary, realm=None):
    r = realm or get_realm()
    merged = {}
    # "all" is the union of both realms: a combined view must not drop the
    # models that only one side's catalog knows about.
    if r == "all":
        source_static = list(wb_catalog.STATIC_INTL_MODELS) + list(wb_catalog.STATIC_CN_MODELS)
    else:
        source_static = getattr(wb_catalog, "STATIC_CN_MODELS" if r == "cn"
                                else "STATIC_INTL_MODELS", wb_catalog.STATIC_MODELS)
    for item in source_static:
        mid = item.get("id")
        # First catalog wins for a shared id, so the intl entry is not
        # overwritten by its cn counterpart when both are merged.
        if mid and is_chat_model(mid) and mid not in merged:
            merged[mid] = dict(item)
    for mid, meta in primary or []:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            base.update(meta)
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    out = []
    if r == "all":
        order = list(INTL_UI_ORDER) + [m for m in CN_UI_ORDER if m not in INTL_UI_ORDER]
    seen = set()
    for mid in order:
        if mid in merged and mid not in seen:
            seen.add(mid)
            out.append((mid, merged[mid]))
    return out


def fetch_endpoint_models(pool=None):
    account = pool.pick(realm="intl") if pool else None
    if account is None:
        log("model discovery skipped: no usable account")
        cached = tr._models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    req = urllib.request.Request(UPSTREAM + MODELS_PATH, method="GET",
                                 headers=account.headers())
    try:
        with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log("model discovery failed: %s" % exc)
        cached = tr._models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    ids, seen = [], set()
    data = payload.get("data") or {}
    for m in data.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else m
        if isinstance(mid, str) and mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    for agent in data.get("agents") or []:
        for mid in agent.get("models") or []:
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids


def fetch_models(realm=None, pool=None):
    r = realm or get_realm()
    with _lock:
        c = tr._models_cache.get(r) or {"at": 0.0, "data": None}
        if c["data"] and time.time() - c["at"] < 300:
            return c["data"]
    live = []
    if r in ("intl", "all"):
        live = [(m, {}) for m in fetch_endpoint_models(pool=pool)]
    entries = merge_catalog(live, realm=r)
    with _lock:
        tr._models_cache[r] = {"at": time.time(), "data": entries}
    return entries


def model_entry(mid, meta):
    """Build a rich /v1/models entry from the desktop app catalog metadata.

    The OpenAI spec only names id/object/created/owned_by, so capability data
    is convention-driven. Several shapes are emitted at once so that different
    clients (OpenRouter-style, LobeChat-style, plain-flag readers) all find
    what they look for.
    """
    meta = meta or {}
    item = {
        "id": mid,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "workbuddy",
    }
    name = meta.get("name")
    if name:
        item["name"] = name
    desc = meta.get("descriptionEn") or meta.get("descriptionZh")
    if desc:
        item["description"] = desc
    # ---- modality / capability ----
    # disabledMultimodal explicitly turns image input off; absent means allowed.
    vision = bool(meta.get("supportsImages")) and not meta.get("disabledMultimodal")
    tools = bool(meta.get("supportsToolCall"))
    thinks = bool(meta.get("supportsReasoning"))
    inputs = ["text"] + (["image"] if vision else [])
    item["capabilities"] = {"vision": vision, "tool_calls": tools, "reasoning": thinks}
    item["supports_vision"] = vision
    item["supports_images"] = vision
    item["supports_tool_calls"] = tools
    item["supports_reasoning"] = thinks
    item["vision"] = vision
    item["multimodal"] = vision
    item["abilities"] = {"vision": vision, "functionCall": tools,
                         "function_call": tools, "reasoning": thinks}
    item["input_modalities"] = inputs
    item["output_modalities"] = ["text"]
    item["modalities"] = {"input": inputs, "output": ["text"]}
    item["architecture"] = {"input_modalities": inputs, "output_modalities": ["text"],
                            "modality": "+".join(inputs) + "->text"}
    # ---- limits ----
    if meta.get("maxInputTokens"):
        item["context_length"] = meta["maxInputTokens"]
        item["max_input_tokens"] = meta["maxInputTokens"]
    if meta.get("maxOutputTokens"):
        item["max_output_tokens"] = meta["maxOutputTokens"]
        item["max_completion_tokens"] = meta["maxOutputTokens"]
    ctx = (meta.get("contextWindow") or {}).get("supportedLengths")
    if ctx:
        item["context_windows"] = ctx
    # ---- reasoning controls ----
    reasoning = meta.get("reasoning") or {}
    efforts = reasoning.get("supportedEfforts")
    if efforts:
        item["reasoning_efforts"] = efforts
    if reasoning.get("effort"):
        item["reasoning_fixed_effort"] = reasoning["effort"]
    if reasoning.get("defaultEffort"):
        item["reasoning_default_effort"] = reasoning["defaultEffort"]
    if reasoning.get("canDisableThinking") is not None:
        item["reasoning_can_disable"] = reasoning["canDisableThinking"]
    # DeepSeek 4.1 official supports low / high / max
    if mid == "deepseek-v4.1-flash":
        item["reasoning_efforts"] = ["low", "high", "max"]
        item["reasoning_default_effort"] = "high"
        item.pop("reasoning_fixed_effort", None)
    if meta.get("onlyReasoning") is not None:
        item["always_reasoning"] = bool(meta.get("onlyReasoning"))
    # ---- misc ----
    if meta.get("credits"):
        item["credits"] = meta["credits"]
    if meta.get("vendor"):
        item["vendor"] = meta["vendor"]
    if meta.get("temperature") is not None:
        item["temperature"] = meta["temperature"]
    if meta.get("top_p") is not None:
        item["top_p"] = meta["top_p"]
    if meta.get("isDefault"):
        item["is_default"] = True
    tags = [t for t in (meta.get("tags") or [])
            if isinstance(t, str) and not t.startswith("badge:")]
    if tags:
        item["tags"] = tags
    return item


# --------------------------------------------------------------------------
# throttle / error classification
# --------------------------------------------------------------------------
class ContentRejected(Exception):
    """Upstream content review rejected this request (403 / code 11140).

    Not an account problem: another credential gets the same 403 for the same
    content, so it is passed straight through instead of cooling the pool.
    """

    def __init__(self, http_error=None, detail=""):
        self.http_error = http_error
        self.detail = detail or ""
        super().__init__("upstream rejected the request content (403)")

    @property
    def code(self):
        return 403


class RateLimited(Exception):
    """Upstream throttled this model (429 / code 6004). Distinct from a dead
    pool: the credential is fine, only the model is cooling down for a while."""

    def __init__(self, http_error=None, detail="", wait=60):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        super().__init__("upstream rate limit: %s" % (self.detail[:200] or "429"))


# ---------------------------------------------------------------------------
# 出站身分自動切換
#
# 官方有兩套身分，端點與配額池都不同：
#   cli        -> codebuddy.ai (國際) / copilot.tencent.com (國內)
#   workbuddy  -> workbuddy.ai  (國際) / workbuddy.cn        (國內)
#
# 某模型在 cli 池被限流（429 / code 6004）時，換成 workbuddy 身分通常
# 還能繼續用 —— 那是另一條配額線。每輪只切一次，避免來回彈跳。
# ---------------------------------------------------------------------------
_SWITCH_LOG = {}


def _switch_count(account, model):
    """這一輪已經切過幾次（60 秒內的切換算同一輪）。"""
    entry = _SWITCH_LOG.get((account.uid, model))
    if not entry:
        return 0
    count, last = entry
    if time.time() - last > 60:
        return 0
    return count


def _try_switch_product(account, model):
    """429 時換身分重試。回傳 True 表示已切換、可以重試。"""
    count = _switch_count(account, model)
    if count >= MAX_PRODUCT_SWITCHES:
        return False
    current = getattr(account, "product", "cli")
    target = "workbuddy" if current == "cli" else "cli"
    try:
        changed = account.set_product(target)
    except Exception as exc:
        log("product switch failed: %s" % exc, level="WARN")
        return False
    if not changed:
        return False
    _SWITCH_LOG[(account.uid, model)] = (count + 1, time.time())
    if len(_SWITCH_LOG) > 500:
        _SWITCH_LOG.clear()
    log("account %s: %s 被限流，自動切換身分 -> %s (第 %d/%d 次)"
        % (account.uid[:8], current, target, count + 1, MAX_PRODUCT_SWITCHES),
        level="WARN")
    return True


def reset_switch_counter(account, model):
    """成功之後歸零，下一次請求重新享有 4 次切換額度。"""
    _SWITCH_LOG.pop((account.uid, model), None)


def retry_after_seconds(model, realm, pool=None):
    """Shortest wait until any account of this realm can serve `model` again."""
    if not pool:
        return 60
    waits = [a.throttle_wait(model=model) for a in pool.accounts
             if a.realm == realm and a.enabled and a.access_token]
    active = [w for w in waits if w > 0]
    return int(min(active)) if active else 60


def realm_model_throttled(realm, model, pool=None):
    """True when accounts exist and are healthy but all are cooling this model.

    Only a *model* cooldown counts. A plain account cooldown usually comes
    from a transient network error, and reporting that as "rate limited" told
    clients to back off from a model that was never throttled.
    """
    if not pool:
        return (False, 0)
    existing = [a for a in pool.accounts
                if a.realm == realm and a.enabled and a.access_token]
    if not existing:
        return (False, 0)
    waits = []
    for a in existing:
        wait = getattr(a, "model_cooldowns", {}).get(model, 0.0) - time.time()
        waits.append(max(0.0, wait))
    if waits and all(w > 0 for w in waits):
        return (True, int(min(waits)))
    return (False, 0)


def is_transient(exc):
    """Network-level flakiness that deserves a retry, not a cooldown.

    Upstream occasionally drops a TLS handshake mid-stream
    (SSL: UNEXPECTED_EOF_WHILE_READING / Remote end closed connection).
    Treating that as a dead account took the only intl account offline for 60s
    and turned one hiccup into a 502 storm.
    """
    t = ("%s %s" % (type(exc).__name__, exc)).lower()
    markers = (
        "ssl", "unexpected_eof", "eof occurred", "remote end closed",
        "connection reset", "connection aborted", "connectionreseterror",
        "connectionabortederror", "timed out", "timeout", "temporarily unavailable",
        "bad gateway", "502", "503", "504", "incompleteread",
    )
    return any(m in t for m in markers)


def parse_rate_limit_reset(detail):
    """Pull the reset time out of an upstream 429 body, if it names one.

    Upstream answers code 6004 with "... your usage will reset at
    2026-09-19 18:29:03 UTC+8 ...". Returns an epoch or None. Kept tolerant on
    purpose: an unparseable body must not break the request path.
    """
    if not detail:
        return None
    m = re.search(r"reset at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", detail)
    if not m:
        return None
    stamp = m.group(1).replace("T", " ")
    tz = re.search(r"UTC([+-]\d{1,2})(?::?(\d{2}))?", detail)
    offset = 0
    if tz:
        hours = int(tz.group(1))
        minutes = int(tz.group(2) or 0)
        offset = hours * 3600 + (minutes * 60 if hours >= 0 else -minutes * 60)
    try:
        base = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) - time.timezone
        return base - offset
    except Exception:
        return None


def open_upstream(pool, payload, session_key=None, target_realm=None, log=log):
    """Pick an account and open the upstream chat stream.

    Returns (response, account). Raises RateLimited / ContentRejected /
    urllib.error.HTTPError / RuntimeError per the classification above.
    Logic preserved line-for-line from wb_proxy.open_upstream; POOL and the
    logger became parameters.
    """
    realm = target_realm or detect_model_realm(payload.get("model")) or get_realm()
    model = str(payload.get("model") or "")
    upstream_body = build_upstream_body(payload)
    # PATCHED-BY-OPS: 客户端未提供会话标识时，用对话稳定前缀兜底。
    # 位置放在 build_upstream_body 之后，保证键与真正发往上游的消息一致
    # （该函数可能在最前面插入 SYSTEM_PROMPT）。
    if not session_key:
        session_key = derive_affinity_key(upstream_body.get("messages"))
        if session_key and AFFINITY_DEBUG:
            log("affinity: derived %s for %d msgs"
                % (session_key, len(upstream_body.get("messages") or [])))
    total = max(1, pool.count_ready(realm, model=model)) if pool else 1
    tried = set()
    last_error = None
    last_uid = None
    last_429 = None
    last_429_detail = ""
    last_403_detail = ""
    transient_hits = 0
    max_attempts = max(2, total) + 1 + (MAX_PRODUCT_SWITCHES if AUTO_SWITCH_PRODUCT else 0)
    for _attempt in range(max_attempts):
        account = pool.pick_for_session(realm=realm, session_key=session_key,
                                        exclude=tried, model=model) if pool else None
        if account is None:
            if transient_hits and _attempt < max_attempts - 1:
                tried.clear()
                time.sleep(min(1.5 * transient_hits, 3.0))
                continue
            break
        if account.realm != realm:
            if session_key and pool:
                pool.affinity.unbind(session_key)
            continue
        tried.add(account.uid)
        last_uid = account.uid
        chat_url = account.chat_base_url() + CHAT_PATH
        # The cache key is account scoped, so it is rebuilt per candidate
        # rather than once up front. Opt-in only: measurement showed the
        # upstream caches prefixes without it (see prompt_cache_key_enabled).
        if prompt_cache_key_enabled():
            attempt_body = inject_prompt_cache_key(upstream_body, account.uid, session_key)
        else:
            attempt_body = upstream_body
        attempt_data = json.dumps(attempt_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(chat_url, data=attempt_data, method="POST",
                                     headers=account.headers(purpose="chat"))
        try:
            resp = wb_accounts.urlopen(req, timeout=600, proxy=account.proxy)
            account.clear_error(model=model)
            reset_switch_counter(account, model)
            return resp, account
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                reset_at = parse_rate_limit_reset(detail)
                wait = max(1.0, reset_at - time.time()) if reset_at else 60.0
                # Model-scoped: only this model is throttled for this account,
                # so sibling models stay serviceable on the same credential.
                account.note_error("HTTP 429 (model throttled)", model=model,
                                   until=reset_at, cooldown=wait)
                if AUTO_SWITCH_PRODUCT and _try_switch_product(account, model):
                    # 換了身分就等於換了一條配額線：要把它從「已試過」拿掉，
                    # 並清掉剛剛記下的模型冷卻，否則下一輪迴圈會找不到帳號。
                    tried.discard(account.uid)
                    try:
                        account.clear_error(model=model)
                    except Exception:
                        pass
                    if session_key and pool:
                        pool.affinity.unbind(session_key)
                    continue
                log("account %s throttled on '%s' (429), retry in %ds"
                    % (account.uid[:8], model, int(wait)))
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                last_error = exc
                last_429 = exc
                last_429_detail = detail
                continue
            if exc.code == 403:
                try:
                    detail = exc.read(400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                log("upstream 403 for '%s' (content review), passing through" % model)
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                last_error = exc
                last_403_detail = detail
                break
            if exc.code == 401:
                log("account %s rejected (HTTP 401), rotating" % account.uid[:8])
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                account.note_error("HTTP 401",
                                   cooldown=60,
                                   single_account=(total <= 1))
                last_error = exc
                continue
            if exc.code in (500, 502, 503, 504):
                transient_hits += 1
                log("upstream %s for '%s', retrying" % (exc.code, model))
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                last_error = exc
                continue
            raise
        except Exception as exc:
            if session_key and pool:
                pool.affinity.unbind(session_key)
            if is_transient(exc):
                transient_hits += 1
                log("upstream connection hiccup for '%s' (%s), retrying"
                    % (model, type(exc).__name__))
                last_error = exc
                time.sleep(min(0.6 * transient_hits, 2.0))
                continue
            account.note_error(str(exc)[:120], cooldown=60, single_account=(total <= 1))
            last_error = exc
            continue
    if last_error is not None:
        # Carry the account that produced the failure out to the caller, so
        # the error row can name it even though the local variable that would
        # have held it was never assigned in the caller.
        try:
            last_error.account_uid = last_uid
        except Exception:
            pass
        if last_429 is not None:
            exc = RateLimited(last_429, last_429_detail,
                              wait=retry_after_seconds(model, realm, pool))
            exc.account_uid = last_uid
            raise exc
        if last_403_detail:
            exc = ContentRejected(last_error, last_403_detail)
            exc.account_uid = last_uid
            raise exc
        raise last_error
    throttled, wait = realm_model_throttled(realm, model, pool)
    if throttled:
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait)
    raise RuntimeError("no usable account for realm '%s': all are disabled, "
                       "cooling down, or expired" % realm)
