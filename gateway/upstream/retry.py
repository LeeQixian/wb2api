"""429/403/5xx classification and identity rotation."""
import time
from gateway import identity as wb_identity



import json
import os
import re
from gateway import settings as wb_settings
from gateway.common.log import log


AUTO_SWITCH_PRODUCT = os.environ.get("WB_AUTO_SWITCH_PRODUCT",
                                     "0").lower() in ("1", "true", "yes", "on")
MAX_PRODUCT_SWITCHES = 4
_SWITCH_LOG = {}
ACCOUNT_SCOPE_PHRASES = (
    "account-level", "account level", "account-wide", "account scope",
    "account quota", "account rate limit", "account limit",
    "per account", "per-account", "this account", "your account",
    "the account", "an account",
    "credential-level", "credential level", "credential scope",
    "credential limit", "per credential", "this credential",
    "the credential", "your credential",
    # Keep the local wording (measured here); merge into the upstream table.
    "too many requests per account",
    "requests per minute per account",
    "hourly cap",
    "账号级", "账户级", "该账号", "此账号", "该账户", "此账户",
    "账号限流", "账户限流", "凭证级", "该凭证",
)
SOFT_LIMIT_MARKERS = ("usage exceeds frequency limit", "too many requests",
                      "请求过于频繁")
SOFT_LIMIT_CODES = (14003,)     # intl, observed on a real 429
RESTRICTED_403_COOLDOWN = 7200.0
_RESTRICTED_403_CODE_RE = re.compile(r'"code"\s*:\s*"?11140"?')
_RESTRICTION_403_MARKERS = ("unusual activity", "账号存在异常行为", "已被限制")


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
    """429 時換身分重試。回傳 True 表示已切換、可以重試。

    同一請求內最多切 MAX_PRODUCT_SWITCHES 次：
      cli -> workbuddy -> cli -> workbuddy
    四次都不行就放棄，讓呼叫端回報真正的 429。
    """
    count = _switch_count(account, model)
    if count >= MAX_PRODUCT_SWITCHES:
        return False
    current = getattr(account, "product", wb_identity.PRODUCT_DESKTOP)
    if current == wb_identity.PRODUCT_DESKTOP:
        target = wb_identity.PRODUCT_VSCODE
    elif current == wb_identity.PRODUCT_VSCODE:
        target = wb_identity.PRODUCT_CLI
    else:
        target = wb_identity.PRODUCT_DESKTOP
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

    Upstream answers code 6004 with a reset wall clock, but the wording is
    per-realm: intl says "... your usage will reset at 2026-09-19 18:29:03
    UTC+8 ...", the cn form says "... 将在 2026-10-09 14:44:59 UTC+8 重置 ...".
    Matching only the English form left every cn 429 without a reset time, so
    it read as scope-less and cooled the wrong thing (hub a351df9). Returns an
    epoch or None. Kept tolerant on purpose: an unparseable body must not
    break the request path.
    """
    if not detail:
        return None
    m = re.search(r"(?:reset at|将在)\s*"
                  r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", detail)
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


def credential_scope_phrase(detail):
    """The scope phrase the body uses, or None when it names no scope.

    ASCII phrases anchor on word boundaries so an identifier like `accountId`
    cannot satisfy one; the Chinese phrases are plain substrings (no word
    boundary exists to anchor to, and they cannot occur inside an identifier).
    """
    lowered = (detail or "").lower()
    for phrase in ACCOUNT_SCOPE_PHRASES:
        if phrase.isascii():
            if re.search(r"\b%s\b" % re.escape(phrase), lowered):
                return phrase
        elif phrase in lowered:
            return phrase
    return None


def rate_limit_code(detail):
    """The `code` field of a JSON 429 body, or None when it is not JSON."""
    try:
        obj = json.loads(detail)
    except Exception:
        return None
    return obj.get("code") if isinstance(obj, dict) else None


def rate_limit_is_soft_shape(detail):
    """True when a 429 body names a rate limit, whatever its scope (hub 72e51b4).

    Covers both shapes the incident environment produced: the bare "usage
    exceeds frequency limit" body (229 of 263 recorded 429s) and the intl code
    14003 body ("too many requests").
    """
    text = (detail or "").lower()
    if any(marker in text for marker in SOFT_LIMIT_MARKERS):
        return True
    return rate_limit_code(detail) in SOFT_LIMIT_CODES


def rate_limit_is_account_level(detail, reset_at):
    """True only with positive evidence that the 429 is credential-scoped.

    A parser miss is evidence of nothing: the credential gets the 600s->2h
    ladder only when the body itself says the limit is on the credential;
    anything else parks one model (hub dc1a565).
    """
    if reset_at is not None:
        return False
    return credential_scope_phrase(detail) is not None


def restriction_is_account_level(detail):
    """True when a 403 body says the ACCOUNT is restricted, not the content.

    code matches on a regex ("11140" as a bare substring can hit a requestId);
    an incomplete body falls back to the content-review path rather than
    mis-parking an account.
    """
    if not detail:
        return False
    if _RESTRICTED_403_CODE_RE.search(detail):
        return True
    lowered = detail.lower()
    return any(marker in lowered for marker in _RESTRICTION_403_MARKERS)
