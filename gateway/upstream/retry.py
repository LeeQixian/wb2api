"""429/403/5xx classification and identity rotation."""
import time
from gateway import identity as wb_identity



import errno
import json
import os
import re
import socket
import urllib.error
from gateway import settings as wb_settings
from gateway.common.log import log


#: Markers of a deterministic certificate/hostname failure.
_TLS_VERIFY_MARKERS = (
    "certificate_verify_failed", "certificate verify failed",
    "self-signed certificate", "self signed certificate",
    "unable to get local issuer certificate", "unable to get issuer certificate",
    "certificate has expired", "hostname mismatch",
)


class TLSVerifyError(RuntimeError):
    """The upstream certificate chain is not trusted by this process."""


def tls_verify_hint(exc=None):
    """Actionable hint naming WB_CA_BUNDLE; appends the raw error when given."""
    hint = ("upstream TLS certificate failed verification (corporate/MITM proxy "
            "or missing root CA); export the signing root CA to PEM and set "
            "WB_CA_BUNDLE=/path/to/ca.pem, or SSL_CERT_FILE, then restart")
    return hint if exc is None else "%s; original error: %s" % (hint, exc)


def is_tls_verify_error(exc):
    """True for cert/hostname failures; checks URLError.reason too.
    These are config errors: retrying repeats them and hides the cause."""
    reason = getattr(exc, "reason", None)
    text = "%s %s %s" % (type(exc).__name__, exc, "" if reason is None else reason)
    lower = text.lower()
    return any(marker in lower for marker in _TLS_VERIFY_MARKERS)


def encode_upstream_body(body):
    """Serialize a chat body like Node JSON.stringify: compact, no ASCII escape.
    Default separators leave spaces that fingerprint every outbound request."""
    return json.dumps(body, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


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
    # Extra scope wordings on top of the upstream error table.
    "too many requests per account",
    "requests per minute per account",
    "hourly cap",
    "账号级", "账户级", "该账号", "此账号", "该账户", "此账户",
    "账号限流", "账户限流", "凭证级", "该凭证",
)
SOFT_LIMIT_MARKERS = ("usage exceeds frequency limit", "too many requests",
                      "请求过于频繁")
SOFT_LIMIT_CODES = (14003,)     # intl soft-limit code on 429 bodies
RESTRICTED_403_COOLDOWN = 7200.0
_RESTRICTED_403_CODE_RE = re.compile(r'"code"\s*:\s*"?11140"?')
_RESTRICTION_403_MARKERS = ("unusual activity", "账号存在异常行为", "已被限制")


def _switch_count(account, model):
    """Switches made this round (switches within 60s count as one round)."""
    entry = _SWITCH_LOG.get((account.uid, model))
    if not entry:
        return 0
    count, last = entry
    if time.time() - last > 60:
        return 0
    return count


def _try_switch_product(account, model):
    """On 429, rotate identity and retry; True means a switch happened.
    At most MAX_PRODUCT_SWITCHES per request, then the real 429 is reported."""
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
    """Zero the counter after a success so the next request gets its budget."""
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
    """True when accounts exist and are healthy but all cool this model.
    Only a model cooldown counts; a plain account cooldown is a hiccup."""
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


def _network_error_details(exc):
    """Wrapped OS error codes plus text, independent of the host's locale."""
    errors = (exc, getattr(exc, "reason", None))
    text = " ".join("%s %s" % (type(error).__name__, error)
                    for error in errors if error is not None).lower()
    # HTTPError inherits OSError but delegates missing attrs to its response,
    # so an HTTP status is not an OS code: never probe it.
    codes = {getattr(error, attr, None) for error in errors
             if not isinstance(error, urllib.error.HTTPError)
             for attr in ("errno", "winerror")}
    # Some callers keep only a string; match the whole number, because the
    # substring "errno 100" would also match Windows ECONNRESET (10054).
    codes.update(int(code) for code in re.findall(
        r"\b(?:errno|winerror)\s+(-?\d+)\b", text))
    return codes, text


def is_transient(exc):
    """Network-level flakiness that deserves a retry, not a cooldown.
    Dropped TLS handshakes must not take an account offline."""
    if is_tls_verify_error(exc):
        return False
    codes, t = _network_error_details(exc)
    # ECONNREFUSED (10061) stays out on purpose: nothing is listening, which
    # usually means a dead per-account proxy; it keeps the charge+cooldown path.
    if codes.intersection((errno.EPIPE, errno.ECONNABORTED, errno.ECONNRESET,
                           errno.ETIMEDOUT, 10053, 10054, 10060)):
        return True
    markers = (
        "ssl", "unexpected_eof", "eof occurred", "remote end closed",
        "connection reset", "connection aborted", "connectionreseterror",
        "connectionabortederror", "timed out", "timeout", "temporarily unavailable",
        "bad gateway", "502", "503", "504", "incompleteread",
    )
    return any(m in t for m in markers)

# Codes naming the model/service, not the credential: 11102 service info,
# 11129 function parameters, 11134 provider, 11150 effort, 11158 availability.
MODEL_SCOPED_ERROR_CODES = frozenset((11102, 11129, 11134, 11150, 11158))


def upstream_error_code(detail):
    """The numeric code field of an upstream error body, or None."""
    try:
        payload = json.loads(detail or "")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("code"))
    except (TypeError, ValueError):
        return None


def http_error_detail(exc, limit=600):
    """Read an HTTPError body once and keep it on the exception for later readers."""
    try:
        raw = exc.read(limit)
    except Exception:
        return ""
    try:
        exc.wb_body = raw
    except Exception:
        pass
    return raw.decode("utf-8", "replace")


def read_http_error(exc, limit=600):
    """The body cached by http_error_detail, or a fresh read."""
    raw = getattr(exc, "wb_body", None)
    if raw is not None:
        return raw[:limit]
    try:
        return exc.read(limit)
    except Exception:
        return b""


def model_scoped_errors_penalize_account():
    """Escape hatch: restore "any 5xx charges the account"."""
    raw = os.environ.get("WB_MODEL_SCOPED_ERRORS_PENALIZE", "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# DNS/route failures also arrive as URLError, but no account ever sent a
# byte; these fingerprints identify "this host has no network" first.
LOCAL_NETWORK_MARKERS = (
    "gaierror", "getaddrinfo failed", "name or service not known",
    "nodename nor servname", "temporary failure in name resolution",
    "network is unreachable", "network is down", "no route to host",
)


def looks_like_local_network(exc):
    """True when the host itself lost DNS/route, not the upstream."""
    if isinstance(exc, socket.gaierror):
        return True
    codes, lower = _network_error_details(exc)
    if codes.intersection((errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH,
                           -2, -3, -5, 100, 101, 113,
                           10050, 10051, 10065, 11001)):
        return True
    return any(marker in lower for marker in LOCAL_NETWORK_MARKERS)


class LocalNetworkError(Exception):
    """Host network down: neither an account failure nor an upstream one."""

    def __init__(self, detail=""):
        self.detail = detail or ""
        super().__init__("local network unavailable: %s"
                         % (self.detail[:200] or "DNS/route failed"))


def exc_detail(exc, limit=180):
    # "URLError" alone hides the cause; .reason is the clean inner text.
    reason = getattr(exc, "reason", None)
    text = str(reason) if reason is not None else str(exc)
    text = (text or "").strip()
    return ("%s: %s" % (type(exc).__name__, text))[:limit] if text else type(exc).__name__


AUTH_CHALLENGE_MARK = "www-authenticate: "


def auth_challenge(headers):
    """The 401's `WWW-Authenticate`, collapsed; APISIX names a dead token there."""
    try:
        value = headers.get("www-authenticate") if headers else ""
    except Exception:
        return ""
    return " ".join(str(value or "").split())[:160]


def http_error_message(exc, detail, limit=None):
    """`HTTP <code>: <body>`; the challenge goes first so truncation keeps it."""
    body = str(detail or "")
    if limit:
        body = body[:limit]
    code = getattr(exc, "code", "")
    challenge = auth_challenge(getattr(exc, "headers", None))
    if not challenge:
        return "HTTP %s: %s" % (code, body)
    return "HTTP %s (%s%s): %s" % (code, AUTH_CHALLENGE_MARK, challenge, body)


def parse_rate_limit_reset(detail):
    """Pull the reset time out of an upstream 429 body, if it names one.
    Code 6004 wordings differ per realm, so intl and cn phrases both match."""
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
    ASCII phrases anchor on word boundaries, so `accountId` cannot match."""
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


CAP_LIMIT_CODE = 6004


def rate_limit_is_cap(detail):
    """True only for the upstream's quota-cap answer (code 6004)."""
    try:
        return int(rate_limit_code(detail)) == CAP_LIMIT_CODE
    except (TypeError, ValueError):
        return False


def rate_limit_is_soft_shape(detail):
    """True when a 429 body names a rate limit, whatever its scope.
    Covers the bare "usage exceeds frequency limit" and intl 14003 wordings."""
    text = (detail or "").lower()
    if any(marker in text for marker in SOFT_LIMIT_MARKERS):
        return True
    return rate_limit_code(detail) in SOFT_LIMIT_CODES


def rate_limit_is_account_level(detail, reset_at):
    """True only with positive evidence that the 429 is credential-scoped.
    The 600s->2h ladder only applies when the body says so."""
    if reset_at is not None:
        return False
    return credential_scope_phrase(detail) is not None


def restriction_is_account_level(detail):
    """True when a 403 body says the ACCOUNT is restricted, not the content.
    An incomplete body falls back to content review instead of parking."""
    if not detail:
        return False
    if _RESTRICTED_403_CODE_RE.search(detail):
        return True
    lowered = detail.lower()
    return any(marker in lowered for marker in _RESTRICTION_403_MARKERS)
