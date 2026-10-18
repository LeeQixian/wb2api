"""Credential parsing, profile fetch and credit-expiry helpers."""

import json
import time
import urllib.request

from gateway.account import jwt as _jwt
from gateway.account import transport as _transport
from gateway.account.web_config import (DAILY_CHAT_MODEL, DAILY_CHAT_WEB_PROMPT,
                                         PROFILE_PATH, WEB_CONVERSATIONS_URL,
                                         WEB_ORIGIN, WEB_ORIGIN_CN,
                                         WEB_TURN_TIMEOUT, WEB_USER_AGENT)
from gateway.account.jwt import (detect_realm_from_token, domain_for_realm,
                                  get_realm_config, jwt_exp, jwt_uid, normalize_epoch)


def web_origin_for(realm):
    """Web-console origin per realm (CN vs international)."""
    return WEB_ORIGIN_CN if str(realm or "").lower() == "cn" else WEB_ORIGIN


def fetch_account_profile(account, timeout=15):
    """Web-console profile -> (uid, nickname); other personal fields dropped.
    A uid mismatch against the credential raises (wrong-account guard)."""
    origin = web_origin_for(getattr(account, "realm", ""))
    req = urllib.request.Request(
        origin + PROFILE_PATH, method="GET",
        headers={
            "Authorization": "Bearer " + str(account.access_token or ""),
            "Accept": "application/json, text/plain, */*",
            "x-client-platform": "web",
            "Origin": WEB_ORIGIN_CN,
            "Referer": WEB_ORIGIN_CN + "/profile/account-settings",
            "User-Agent": WEB_USER_AGENT,
        },
    )
    with _transport.urlopen(req, timeout=timeout, proxy=getattr(account, "proxy", "")) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("profile response is not an object")
    uid = str(data.get("uid") or "")
    if uid and account.uid and uid != account.uid:
        raise RuntimeError("profile uid mismatch")
    nickname = data.get("nickname")
    return uid, nickname.strip() if isinstance(nickname, str) else ""


def deduction_end_text(acc):
    """Upstream DeductionEndTime (ms epoch) as text; "" when missing.
    CycleEndTime is only a billing boundary, so callers may prefer it instead."""
    raw = acc.get("DeductionEndTime")
    if raw in (None, "", 0, "0"):
        return ""
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return ""
    if stamp > 1e11:
        stamp /= 1000.0
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    except (OSError, ValueError):
        return ""


def credential_to_kwargs(blob, realm=None):
    """Normalize a credential (desktop .info / accounts/*.json / raw JWT).
    Missing fields are decoded from the token; `realm` overrides the issuer."""
    if isinstance(blob, (bytes, bytearray)):
        blob = blob.decode("utf-8", "replace")
    if isinstance(blob, str):
        text = blob.strip()
        blob = json.loads(text) if text.startswith("{") else {"accessToken": text}
    if not isinstance(blob, dict):
        raise ValueError("credential must be a JWT string or a JSON object")
    auth = blob.get("auth") if isinstance(blob.get("auth"), dict) else {}
    prof = blob.get("account") if isinstance(blob.get("account"), dict) else {}
    token = str(blob.get("accessToken") or auth.get("accessToken") or "").strip()
    if not token or token.count(".") < 2:
        raise ValueError("no JWT accessToken found in the credential")
    uid = str(blob.get("uid") or prof.get("uid") or jwt_uid(token) or "")
    if not uid:
        raise ValueError("token has no sub claim and no uid field; cannot key the account")
    enterprise = str(blob.get("enterpriseId") or blob.get("enterpriseID")
                     or prof.get("enterpriseId") or prof.get("enterpriseID") or "")
    domain = str(blob.get("domain") or auth.get("domain") or "")
    # an explicit realm field (older proxies store one) wins over inference
    declared = str(blob.get("realm") or auth.get("realm") or "").strip().lower()
    detected = realm or (declared if declared in ("intl", "cn") else None) \
        or detect_realm_from_token(token, domain)
    # token evidence outranks a stale file domain for X-Domain too
    domain = domain_for_realm(detected, domain) or get_realm_config(detected)["domain"]
    cfg = get_realm_config(detected)
    return {
        "uid": uid,
        "nickname": str(blob.get("nickname") or prof.get("nickname") or "") or uid[:8],
        "domain": domain or cfg["domain"],
        "realm": detected,
        "platform": "CLI",
        "enterpriseId": enterprise,
        "accessToken": token,
        "refreshToken": str(blob.get("refreshToken") or auth.get("refreshToken") or ""),
        "expiresAt": normalize_epoch(blob.get("expiresAt") or auth.get("expiresAt"))
                     or jwt_exp(token),
        "source": "import",
        "enabled": True,
    }
