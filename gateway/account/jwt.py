"""JWT decoding, realm evidence and per-realm endpoint config."""

import base64
import json
import time
import urllib.request

from gateway import settings as wb_settings
from gateway.config import identity as wb_identity

REALM_CONFIGS = {
    "intl": {
        "name": "国际版 (Global)",
        "chat_upstream": "https://www.workbuddy.ai",
        "billing_upstream": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        # Single source: wb_identity (intl chat UA was once left stale here).
        "chat_ua": wb_identity.DESKTOP_UA_INTL,
        "billing_ua": "WorkBuddy/%s" % wb_identity.DESKTOP_VERSION_INTL,
        "has_checkin": False,
    },
    "cn": {
        "name": "国内版 (China)",
        "chat_upstream": "https://copilot.tencent.com",
        "billing_upstream": "https://www.codebuddy.cn",
        "origin": "https://www.codebuddy.cn",
        "domain": "copilot.tencent.com",
        "chat_ua": wb_identity.DESKTOP_UA_CN,
        "billing_ua": "WorkBuddy/%s" % wb_identity.DESKTOP_VERSION_CN,
        "has_checkin": True,
    },
}

AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
GET_RESOURCE_PATH = "/v2/billing/meter/get-user-resource"
ENTERPRISE_USAGE_PATH = "/billing/meter/get-enterprise-user-usage"

LOGIN_PENDING = 11217
LOGIN_TTL_SECONDS = 600
USER_AGENT = REALM_CONFIGS["intl"]["chat_ua"]

CN_REALM_MARKERS = ("copilot.tencent.com", "codebuddy.cn", "workbuddy.cn")
INTL_REALM_MARKERS = ("workbuddy.ai", "codebuddy.ai")
REALM_MARKERS = {"cn": CN_REALM_MARKERS, "intl": INTL_REALM_MARKERS}

#: upstream uses a far-future deduction end to mean "never expires".
EXPIRY_SENTINEL_DAYS = 730


def get_realm_config(realm):
    """Endpoint config for a realm, defaulting to intl."""
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["intl"]


def _jwt_claims(token):
    """Decode the JWT payload; empty dict on any failure."""
    try:
        segment = str(token).split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception:
        return {}


def jwt_exp(token):
    try:
        return int(_jwt_claims(token).get("exp") or 0)
    except Exception:
        return 0


def jwt_uid(token):
    return str(_jwt_claims(token).get("sub") or "")


def jwt_issuer(token):
    return str(_jwt_claims(token).get("iss") or "")


def normalize_epoch(value):
    """Seconds from a value that may be ms; 0 when unparsable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1e11:
        number /= 1000.0
    return int(number)


def realm_evidence(token, domain=None):
    """Return "cn"/"intl" from the token issuer, then the domain; None unclear.
    The issuer outranks the domain, which may be stale or the other realm's."""
    iss = jwt_issuer(token).lower()
    if any(marker in iss for marker in CN_REALM_MARKERS):
        return "cn"
    if any(marker in iss for marker in INTL_REALM_MARKERS):
        return "intl"
    dom = str(domain or "").lower()
    if any(marker in dom for marker in CN_REALM_MARKERS):
        return "cn"
    if any(marker in dom for marker in INTL_REALM_MARKERS):
        return "intl"
    return None


def detect_realm_from_token(token, domain=None):
    """Realm from evidence, falling back to intl."""
    return realm_evidence(token, domain) or "intl"


def domain_for_realm(realm, domain):
    """Align the domain to the realm; keep it when the realm is unclear."""
    dom = str(domain or "").strip()
    other = "intl" if realm == "cn" else "cn"
    if dom and not any(marker in dom.lower() for marker in REALM_MARKERS[other]):
        return dom
    return get_realm_config(realm)["domain"]
