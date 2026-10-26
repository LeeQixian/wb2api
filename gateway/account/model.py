"""Account model: credentials, outbound headers, cooldowns and credit state."""

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from gateway.fingerprint import derive_id, generate_request_id, session_id
from gateway import identity
from gateway import settings as wb_settings
from gateway.account import jwt as _jwt
from gateway.account import backoff as _backoff
from gateway.account.affinity import _human_delta, _human_left
from gateway.account.backoff import (BREAKER_THRESHOLD, DEGRADE_THRESHOLD,
                                     FAIL_WINDOW, breaker_backoff,
                                     degrade_backoff, next_local_4am,
                                     soft_backoff, unverified_soft_backoff)
from gateway.account.credentials import deduction_end_text
from gateway.account.jwt import (EXPIRY_SENTINEL_DAYS, GET_RESOURCE_PATH,
                                 REFRESH_PATH, detect_realm_from_token,
                                 domain_for_realm, get_realm_config, jwt_exp,
                                 jwt_uid, normalize_epoch)
from gateway.account.maintenance import MaintenanceMixin
from gateway.account import transport as _transport
from gateway.common.log import log


def _auth_failure_text(text):
    """True for a credential-shaped 401, not an intermediate proxy page.
    An HTML 401 from nginx/openresty must not park a healthy token (#440)."""
    lower = str(text or "").lower()
    if "<html" in lower:
        return False
    return ("401" in lower or "unauthorized" in lower or "authorization" in lower
            or "token expired" in lower or "invalid_token" in lower)


#: rewritten from scratch each run; never persisted.
RUNTIME_ONLY_FIELDS = ("lastError", "cooldownUntil")


def _restore_model_windows(raw):
    """429 windows that have not expired yet survive a restart (hub a6bb76a).
    Accepts {model: epoch} (what to_dict writes) and a list of rows."""
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = [(row.get("model"), row.get("expiresAt"))
                 for row in raw if isinstance(row, dict)]
    else:
        items = []
    now = time.time()
    windows = {}
    for model, until in items:
        try:
            deadline = float(until)
        except (TypeError, ValueError):
            continue
        name = str(model or "").strip()
        if name and deadline > now:
            windows[name] = deadline
    return windows

#: persisted but reset on load (runtime bookkeeping).
VOLATILE_FIELDS = ("soft_rate_streak", "breaker_fails", "degrade_fails",
                   "model_cooldowns")


class Account(MaintenanceMixin):
    """One WorkBuddy credential with its runtime state."""

    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        token = str(data.get("accessToken") or "")
        self.uid = str(data.get("uid") or jwt_uid(token))
        # CN desktop nicknames arrive as {"$wbEncrypted": ...}; keep plain
        # strings only so a dict never lands in the account row.
        raw_nickname = data.get("nickname")
        self.nickname = (raw_nickname.strip()
                         if isinstance(raw_nickname, str) and "$wbEncrypted" not in raw_nickname
                         else "")
        self.domain = str(data.get("domain") or "")
        self.realm = str(data.get("realm") or detect_realm_from_token(token, self.domain))
        # a stale domain that names the OTHER realm would drag X-Domain
        # back to the wrong exit; align it with the token-resolved realm.
        self.domain = domain_for_realm(self.realm, self.domain)
        self.platform = str(data.get("platform") or "CLI")
        # Persisted identity: manual/429 switches survive a restart, invalid
        # or missing values fall back to the desktop identity.
        self.product = identity.normalize_product(data.get("product"))
        self.enterprise_id = str(data.get("enterpriseId") or "")
        self.access_token = token
        self.refresh_token = str(data.get("refreshToken") or "")
        self.expires_at = normalize_epoch(data.get("expiresAt")) or jwt_exp(token)
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source") or "oauth")
        self.proxy = str(data.get("proxy") or "").strip()
        self.enabled = data.get("enabled", True)
        # Runtime-only, never restored from the credential file:
        # a persisted error outlived its 429 across restarts. save() strips it.
        self.last_error = ""
        # Set only by a credential-shaped failure; an HTML proxy 401 never sets it.
        self.token_expired = not token
        self.cooldown_until = 0.0
        # Per-model cooldowns: upstream 6004 rate limits bind one model, not
        # the account, so other models keep serving. Absolute 429 reset
        # instants are persisted in the credential and re-armed on load;
        # throttle_wait compares against the clock, so they lapse on their own.
        self.model_cooldowns = _restore_model_windows(data.get("modelWindows"))
        self.credits = data.get("credits") or None
        self.last_checkin = data.get("lastCheckin") or None
        self.last_daily_chat = data.get("lastDailyChat") or None
        # Stop handing this account out at this balance so it never reaches
        # zero, which makes upstream send nagging SMS. 0 disables the guard.
        self.reserve_credits = 0
        # Skip accounts that already burned this many tokens today so clients
        # rotate instead of hitting the upstream wall. 0 disables the guard.
        self.daily_token_limit = 0
        self.daily_tokens_today = None
        # 402 hard park until the next local 04:00, lifted early by a
        # balance refresh that shows credits again. Runtime-only.
        self.balance_until = 0.0
        # Prefer accounts whose soonest-expiring credits fall inside this many
        # days, so credits about to lapse get spent first. 0 disables it.
        self.expiring_window_days = 0
        # Failure streaks: soft 429, breaker and degrade counters.
        # Runtime-only: a restart clears them.
        self.soft_streak = 0
        # Unscoped-429 counter, deliberately separate from soft_streak so the
        # two ladders never feed each other.
        self.unscoped_streak = 0
        self.fails = 0
        self.degrade_count = 0
        self.breaker_until = 0.0
        self.degrade_until = 0.0
        # Timestamp of the last charged failure; FAIL_WINDOW decays off it.
        self.last_fail_at = 0.0
        # Last soft 429: both rate ladders share FAIL_WINDOW through it.
        self.last_soft_at = 0.0
        # Upstream rotates the refresh token, so concurrent refresh()/save()
        # must not let a stale snapshot overwrite a freshly minted token.
        self._refresh_lock = threading.Lock()
        self._save_lock = threading.Lock()
        # Health/list snapshots read this state while request threads update
        # it. Keep it separate from _refresh_lock, which spans network requests.
        self._throttle_lock = threading.Lock()
        # Guards fetch_credits(): two overlapping calls would burn two billing
        # requests and race to publish whichever answer lands second.
        self._credits_lock = threading.Lock()
        # Guards the local estimate consume_credit() writes. Separate from
        # _credits_lock, which is held across a network call.
        self._balance_lock = threading.Lock()
        # Live web sessions this account created: {conversation id: start}.
        # The turn thread's finally cannot free a session when the thread
        # itself is wedged in socket land; the scheduler sweeps this ledger.
        self._web_sessions = {}
        self._web_sessions_lock = threading.Lock()

    def to_dict(self):
        now = time.time()
        with self._throttle_lock:
            windows = {model: int(until)
                       for model, until in self.model_cooldowns.items()
                       if until > now}
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "enterpriseId": self.enterprise_id,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "addedAt": self.added_at,
            "source": self.source,
            "proxy": self.proxy,
            "enabled": self.enabled,
            "lastError": self.last_error,
            "cooldownUntil": self.cooldown_until,
            "modelWindows": windows,
            "credits": self.credits,
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
        }

    def _throttle_snapshot(self, now):
        """Return one consistent view of account and per-model cooldowns."""
        with self._throttle_lock:
            error = self.last_error
            deadline = self.cooldown_until
            active = [(model, until) for model, until in self.model_cooldowns.items()
                      if until > now]
        active.sort(key=lambda pair: (pair[1], pair[0]))
        models = [{"model": model, "expiresAt": int(until)} for model, until in active]
        return error, deadline, models

    def model_cooldowns_snapshot(self):
        """Active model cooldowns, ordered by recovery time (epoch seconds)."""
        return self._throttle_snapshot(time.time())[2]

    def reserve_blocked(self):
        """Low-credit guard: idle this account; only a *known* balance blocks."""
        reserve = int(self.reserve_credits or 0)
        if reserve <= 0:
            return False
        credits = self.credits
        if not isinstance(credits, dict):
            return False
        remain = credits.get("remain")
        if remain is None:
            return False
        try:
            remain = int(remain)
        except (TypeError, ValueError):
            return False
        return remain <= reserve

    def consume_credit(self, amount):
        """Subtract one request's cost from the cached balance.
        The refresher lags, so this keeps `reserve_blocked` honest between ticks."""
        try:
            amount = float(amount or 0)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        with self._balance_lock:
            credits = self.credits
            if not isinstance(credits, dict):
                return
            try:
                remain = float(credits.get("remain") or 0)
                used = float(credits.get("used") or 0)
                size = float(credits.get("size") or 0)
            except (TypeError, ValueError):
                return
            remain = max(0.0, remain - amount)
            used += amount
            credits["remain"] = round(remain, 2)
            credits["used"] = round(used, 2)

    def daily_limit_blocked(self):
        """Daily token guard: block once today's counted usage hits the limit."""
        try:
            limit = int(self.daily_token_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return False
        used = self.daily_tokens_today
        if used is None:
            return False
        try:
            return int(used) >= limit
        except (TypeError, ValueError):
            return False

    def public(self):
        exp = self.expires_at or jwt_exp(self.access_token)
        now = time.time()
        last_error, deadline, models = self._throttle_snapshot(now)
        return {
            "uid": self.uid,
            "nickname": self.nickname or (self.uid[:8] if self.uid else "?"),
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "product": self.product,
            "enabled": bool(self.enabled),
            "proxy": self.proxy,
            "expiresAt": exp,
            "expiresIn": _human_delta(exp - time.time()) if exp else None,
            "hasRefreshToken": bool(self.refresh_token),
            "lastError": last_error,
            "inCooldown": deadline > now,
            "cooldownFor": round(max(0.0, deadline - now)) or None,
            "modelCooldowns": models,
            "credits": self.credits,
            "reserveCredits": int(self.reserve_credits or 0),
            "reserveBlocked": self.reserve_blocked(),
            "dailyTokenLimit": int(self.daily_token_limit or 0),
            "dailyTokensToday": (int(self.daily_tokens_today)
                                 if isinstance(self.daily_tokens_today, int)
                                 else None),
            "dailyLimitBlocked": self.daily_limit_blocked(),
            "lastCheckin": self.last_checkin,
            "lastDailyChat": self.last_daily_chat,
            "canCheckin": self.realm == "cn" and not self.is_enterprise(),
            "canDailyChat": self.realm == "intl",
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": session_id(self.uid),
            # Distance to the breaker and last strike age, for wbctl status.
            "fails": int(self.fails),
            "failWindow": int(FAIL_WINDOW),
            "lastFailFor": (round(max(0.0, now - self.last_fail_at))
                            if self.last_fail_at else None),
        }

    def save(self, directory):
        # Private credential: 0700 dir, 0600 file, no group/world read window.
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.uid or "")).strip("_ ")
        name = (safe_uid or uuid.uuid4().hex) + ".json"
        path = os.path.abspath(os.path.join(directory, name))
        if not path.startswith(os.path.abspath(directory)):
            raise ValueError("invalid path for account save")
        with self._save_lock:
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                payload = self.to_dict()
                # Live error/cooldown state never reaches the credential file
                # (see RUNTIME_ONLY_FIELDS).
                for field in RUNTIME_ONLY_FIELDS:
                    payload.pop(field, None)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        self.path = path
        return path

    def delete(self):
        if self.path and os.path.exists(self.path):
            os.remove(self.path)

    def ready(self, model=None, allow_refresh=True):
        if not self.enabled or not self.access_token:
            return False
        if self.token_expired and not (allow_refresh and self.refresh()):
            # A dead credential only comes back through a successful refresh.
            return False
        if self.throttle_wait(model=model) > 0:
            return False
        # Parked below the reserve: serving a request here is what would push
        # the balance to zero and trigger the upstream reminder SMS.
        if self.reserve_blocked():
            return False
        # Today's token budget is spent: keep the seat for tomorrow instead
        # of letting the upstream answer 429 for the rest of the day.
        if self.daily_limit_blocked():
            return False
        exp = self.expires_at or jwt_exp(self.access_token)
        if not exp:
            return True
        remaining = exp - time.time()
        if remaining > 120:
            return True
        # Refresh decides availability: returning True unconditionally sent
        # stale-credential requests upstream. Read-only callers disable it.
        if not allow_refresh:
            return False
        return self.refresh()

    def unavailable_reason(self, model=None):
        """One-line reason this account cannot take work ('' if it can)."""
        if not self.enabled:
            return "disabled"
        if not self.access_token:
            return "no credential"
        now = time.time()
        penalties = [("breaker", self.breaker_until),
                     ("degraded", self.degrade_until),
                     ("balance park", self.balance_until),
                     ("soft-rate cooldown", self.cooldown_until)]
        if model:
            penalties.append(("cooldown for model %s" % model,
                              self.model_cooldowns.get(model, 0.0)))
        label, until = max(penalties, key=lambda item: item[1])
        if until > now:
            # Seconds, not minutes: "0 min left" reads as already usable.
            streak = ""
            if label == "soft-rate cooldown" and self.soft_streak > 1:
                streak = " (#%d in a row)" % self.soft_streak
            elif label.startswith("cooldown for model") and self.unscoped_streak > 1:
                streak = " (#%d in a row)" % self.unscoped_streak
            return "%s (%s left%s)" % (label, _human_left(until - now), streak)
        if self.reserve_blocked():
            return "balance below reserve"
        if self.daily_limit_blocked():
            return "daily token limit reached"
        exp = self.expires_at or jwt_exp(self.access_token)
        if self.token_expired or (exp and exp - now <= 120):
            return "credential expired"
        return ""

    def is_cli_identity(self):
        """Whether outbound identity is the official CLI (decides chat wire form)."""
        return self.product == identity.PRODUCT_CLI

    def headers(self, purpose="chat", session_key=None, turn_request_id=None,
                model=None, conversation_id=None):
        """Chat uses the identity header set; billing stays light. Chat never
        sends X-Machine-ID / X-Session-ID; both derive per account."""
        cfg = get_realm_config(self.realm)

        if purpose != "chat":
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": cfg["billing_ua"],
                "Origin": cfg["origin"],
                "Referer": cfg["origin"] + "/",
                "Authorization": "Bearer " + self.access_token,
                "X-User-Id": self.uid,
                "X-Domain": self.domain or cfg["domain"],
                "X-CodeBuddy-Request": "1",
                "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
                "X-Request-ID": generate_request_id(self.uid),
                "X-Machine-ID": derive_id(self.uid, "machine"),
                "X-Session-ID": session_id(self.uid),
            }
            if self.enterprise_id:
                headers["X-Enterprise-Id"] = self.enterprise_id
                headers["X-Tenant-Id"] = self.enterprise_id
            else:
                headers["X-No-Enterprise-Id"] = "1"
            if self.realm == "cn":
                headers["X-Product"] = "SaaS"
            return headers

        identity_headers = identity.build_identity_headers(
            product=self.product,
            realm=self.realm,
            uid=self.uid,
            token=self.access_token,
            conversation_id=conversation_id,
            enterprise_id=self.enterprise_id,
            tenant_id=self.enterprise_id,
            model=model,
            session_key=session_key,
            turn_request_id=turn_request_id,
        )
        if self.is_cli_identity() or (
                self.product == identity.PRODUCT_DESKTOP
                and identity.desktop_chat_compat_enabled()):
            # Official CLI chat mimics the OpenAI Node SDK: no browser headers.
            # Opt-in desktop chat uses the same SDK profile, not the web one.
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": cfg["origin"],
                "Referer": cfg["origin"] + "/",
                "X-CodeBuddy-Request": "1",
                "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
            }
        headers.update(identity_headers)
        return headers

    def set_product(self, value):
        """Switch outbound identity (cli <-> workbuddy); True when it changed."""
        new = identity.normalize_product(value)
        if new == self.product:
            return False
        self.product = new
        try:
            self.clear_error()
        except Exception:
            pass
        return True

    def chat_base_url(self):
        """Chat endpoint for this account's current identity."""
        return identity.endpoint_for(self.realm, self.product)[0]

    def refresh(self):
        with self._refresh_lock:
            return self._refresh_locked()

    def _refresh_locked(self):
        if not self.refresh_token:
            self.token_expired = True
            self._set_last_error("no refresh token; sign in again")
            return False
        cfg = get_realm_config(self.realm)
        url = cfg["chat_upstream"] + REFRESH_PATH
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
            "X-Refresh-Token": self.refresh_token,
            "X-Auth-Refresh-Source": "workbuddy" if self.realm == "cn" else "plugin",
            "X-User-Id": self.uid,
            "X-Domain": self.domain or cfg["domain"],
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
        }
        if self.enterprise_id:
            headers["X-Enterprise-Id"] = self.enterprise_id
        try:
            payload = _transport.http_json(url, data=b"{}", method="POST", headers=headers, timeout=30,
                                proxy=self.proxy)
        except Exception as exc:
            if _auth_failure_text(exc):
                self.token_expired = True
                self._set_last_error("refresh rejected (401); sign in again")
            else:
                self._set_last_error("refresh failed: %s" % exc)
            return False
        data = (payload.get("data") or {})
        data = data.get("data") or data
        token = data.get("accessToken")
        if not token:
            self.token_expired = True
            self._set_last_error("refresh returned no token (%s)" % payload.get("msg"))
            return False
        self.access_token = token
        self.refresh_token = data.get("refreshToken") or self.refresh_token
        self.expires_at = jwt_exp(token) or self.expires_at
        self.token_expired = False
        with self._throttle_lock:
            self.last_error = ""
            self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return True

    def fetch_credits(self):
        """Refresh the balance; credits again lifts an early 402 park."""
        with self._credits_lock:
            return self._fetch_credits_locked()

    def _fetch_credits_locked(self):
        cfg = get_realm_config(self.realm)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": now,
            "PackageEndTimeRangeEnd": "2036-01-01 00:00:00",
        }
        url = cfg["billing_upstream"] + GET_RESOURCE_PATH
        headers = self.headers(purpose="billing")
        try:
            # One short attempt: /credits fans out per account, so retries
            # would hold handler threads for minutes.
            res = _transport.http_json(url, data=json.dumps(body).encode(), method="POST", headers=headers,
                            timeout=10, retries=1, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        data = res.get("data", {}).get("Response", {}).get("Data", {})
        accounts = data.get("Accounts") or []
        # Packages go through the hub's parser to carry days_left/no_expiry;
        # the expiring-credits preference reads those fields.
        packages = [self._parse_package_account(a) for a in accounts]
        tot_remain = round(sum(p["remain"] for p in packages), 2)
        tot_used = round(sum(p["used"] for p in packages), 2)
        tot_size = round(sum(p["size"] for p in packages), 2)
        active = [p for p in packages
                  if p.get("remain", 0) > 0 and not p.get("is_expired")
                  and p.get("days_left") is not None]
        active.sort(key=lambda p: p["days_left"])
        earliest_expiring = None
        if active:
            ep = active[0]
            earliest_expiring = {
                "name": ep["name"],
                "package_code": ep.get("package_code", ""),
                "remain": ep["remain"],
                "cycle_end_time": ep["cycle_end_time"],
                "days_left": ep["days_left"],
            }
        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "earliest_expiring": earliest_expiring,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        self.revive_balance_cooldown()
        return {"ok": True, "credits": self.credits}

    def soonest_expiring_days(self):
        """Days left on the earliest still-usable package; None when unknown."""
        credits = self.credits
        if not isinstance(credits, dict):
            return None
        soonest = None
        for package in (credits.get("packages") or []):
            if not isinstance(package, dict):
                continue
            # package_code is a fallback for credit blobs written before the
            # no_expiry flag existed.
            if package.get("no_expiry"):
                continue
            if package.get("package_code") == "enterprise":
                continue
            if package.get("is_expired"):
                continue
            days = package.get("days_left")
            remain = package.get("remain")
            if not isinstance(days, (int, float)) or days < 0:
                continue
            if not isinstance(remain, (int, float)) or remain <= 0:
                continue
            if soonest is None or days < soonest:
                soonest = float(days)
        return soonest

    def in_expiring_window(self):
        """True when this account should jump the dispatch queue."""
        try:
            window = int(self.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            return False
        soonest = self.soonest_expiring_days()
        return soonest is not None and soonest <= window

    def _parse_package_account(self, acc):
        pkg_name = acc.get("PackageName") or "Package"
        pkg_code = acc.get("PackageCode") or ""

        def _val(*keys):
            for k in keys:
                v = acc.get(k)
                if v is not None and v != "":
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        pass
            return 0.0

        size = _val("CycleCapacitySizePrecise", "CycleCapacitySize", "CapacitySizePrecise", "CapacitySize")
        remain = _val("CycleCapacityRemainPrecise", "CycleCapacityRemain", "CapacityRemainPrecise", "CapacityRemain")
        used = _val("CycleCapacityUsedPrecise", "CycleCapacityUsed", "CapacityUsedPrecise", "CapacityUsed")

        if size > 0 and used <= 0 and remain <= size:
            used = max(0.0, size - remain)
        elif size > 0 and remain <= 0 and used < size:
            remain = max(0.0, size - used)

        # Extract the grant reason (official campaign, referral, ...).
        grant_reason = ""
        for attr in (acc.get("AccountAttributes") or []):
            if isinstance(attr, dict) and attr.get("Key") == "grantReason":
                grant_reason = str(attr.get("Value") or "")
                break

        # Extract the creation time (ms timestamp).
        create_time = ""
        raw_create = acc.get("CreateTime")
        if raw_create:
            try:
                ts = float(raw_create)
                if ts > 1e11:
                    ts /= 1000.0
                create_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except Exception:
                create_time = str(raw_create)

        end_time_str = acc.get("CycleEndTime") or acc.get("ExpiredTime") or ""
        # Follow DeductionEndTime, not CycleEndTime: for free/trial packs the
        # latter would flag an expiry they do not have.
        expire_time_str = deduction_end_text(acc) or end_time_str
        days_left = None
        is_expired = False
        no_expiry = False
        if expire_time_str:
            try:
                clean_time = expire_time_str.replace("T", " ")[:19]
                end_ts = time.mktime(time.strptime(clean_time, "%Y-%m-%d %H:%M:%S"))
                diff_sec = end_ts - time.time()
                if diff_sec > EXPIRY_SENTINEL_DAYS * 86400.0:
                    no_expiry = True
                else:
                    days_left = round(diff_sec / 86400.0, 1)
                    is_expired = diff_sec < 0
            except Exception:
                pass

        in_usage = bool(acc.get("InUsage"))
        if not in_usage and not is_expired and remain > 0 and used > 0:
            in_usage = True

        return {
            "name": pkg_name,
            "package_code": pkg_code,
            "product_name": acc.get("ProductName") or "",
            "sub_product_name": acc.get("SubProductName") or "",
            "grant_reason": grant_reason,
            "resource_id": acc.get("ResourceId") or "",
            "deal_name": acc.get("DealName") or "",
            "create_time": create_time,
            "remain": round(remain, 2),
            "used": round(used, 2),
            "size": round(size, 2),
            "unit": acc.get("CapacityUnit") or "credits",
            "in_usage": in_usage,
            "auto_renew": bool(acc.get("AutoRenewFlag") or acc.get("SupportAutoRenew")),
            "cycle_start_time": acc.get("CycleStartTime") or "",
            "cycle_end_time": end_time_str,
            "expire_time": expire_time_str,
            "no_expiry": no_expiry,
            "days_left": days_left,
            "is_expired": is_expired,
            "status": acc.get("Status", 0),
        }

    def note_balance_cooled(self, message="insufficient credits"):
        """402 / out of credits: park until the next local 04:00."""
        until = next_local_4am()
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.balance_until = max(self.balance_until, until)
        return until

    def revive_balance_cooldown(self):
        """A refresh showing credits again lifts the 402 park, nothing else."""
        if not self.balance_until:
            return False
        credits = self.credits if isinstance(self.credits, dict) else {}
        try:
            remain = int(credits.get("remain"))
        except (TypeError, ValueError):
            remain = None
        if remain is None or remain <= 0:
            return False
        with self._throttle_lock:
            self.balance_until = 0.0
            self.last_error = ""
            self.last_error_detail = ""
        return True

    def note_soft_rate(self, message):
        """Account-level rate limit: soft cooldown with exponential backoff."""
        with self._throttle_lock:
            self._decay_soft_streak()
            self.soft_streak += 1
            wait = soft_backoff(self.soft_streak)
            self.last_error = str(message)[:200]
            self.cooldown_until = max(self.cooldown_until, time.time() + wait)
        return wait

    def note_unscoped_rate(self, model):
        """Unscoped soft 429: grow its own streak, return this model's window."""
        with self._throttle_lock:
            self._decay_soft_streak()
            self.unscoped_streak += 1
            return unverified_soft_backoff(self.unscoped_streak)

    def _decay_strikes(self):
        """Drop strikes older than FAIL_WINDOW; caller holds _throttle_lock."""
        now = time.time()
        if FAIL_WINDOW and self.last_fail_at and now - self.last_fail_at > FAIL_WINDOW:
            self.fails = 0
            self.degrade_count = 0
        self.last_fail_at = now

    def _decay_soft_streak(self):
        """Same window for both rate ladders; caller holds _throttle_lock.
        A 429 a day later starts at the first rung, not at yesterday's."""
        now = time.time()
        if FAIL_WINDOW and self.last_soft_at and now - self.last_soft_at > FAIL_WINDOW:
            self.soft_streak = 0
            self.unscoped_streak = 0
        self.last_soft_at = now

    def note_failure(self, message):
        """5xx / transport failure: feed the breaker counter."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self._decay_strikes()
            self.fails += 1
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                self.breaker_until = max(self.breaker_until, time.time() + wait)
        return self.breaker_until

    def note_unknown_failure(self, message):
        """Unknown error: degrade counter plus the shared breaker counter."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self._decay_strikes()
            self.degrade_count += 1
            self.fails += 1
            now = time.time()
            if self.degrade_count >= DEGRADE_THRESHOLD:
                wait = degrade_backoff(self.degrade_count)
                self.degrade_until = max(self.degrade_until, now + wait)
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                self.breaker_until = max(self.breaker_until, now + wait)
        return self.degrade_until

    def note_success(self, model=None):
        """A served request clears every account-level penalty."""
        dropped = False
        with self._throttle_lock:
            if model:
                dropped = self.model_cooldowns.pop(model, None) is not None
            self.soft_streak = 0
            self.unscoped_streak = 0
            self.fails = 0
            self.degrade_count = 0
            self.last_fail_at = 0.0
            self.last_soft_at = 0.0
            self.breaker_until = 0.0
            self.degrade_until = 0.0
            self.last_error = ""
            self.last_error_detail = ""
            self.cooldown_until = 0.0
            # A credential-shaped 401 parks the account (ready() drops it), but
            # the token demonstrably works now (hub 392410b): lift it here
            # instead of waiting for a refresh or a restart.
            self.token_expired = False
        if dropped:
            # Only when a window really existed: this path runs per request.
            self._persist_model_windows()

    def _set_last_error(self, message):
        """Record a refresh error in the account's runtime state."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None):
        persist_window = False
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            if _auth_failure_text(message):
                self.token_expired = True
            if model:
                # Model-scoped: the account stays usable for every other model.
                wait = max(1.0, float(until) - time.time()) if until else (
                    3.0 if single_account else float(cooldown))
                self.model_cooldowns[model] = time.time() + wait
                persist_window = True
            else:
                actual_cooldown = 3 if single_account else cooldown
                self.cooldown_until = time.time() + actual_cooldown
        if persist_window:
            # A 429 reset instant outlives the process; without this a restart
            # both forgets the park and re-serves the exhausted window.
            self._persist_model_windows()

    def _persist_model_windows(self):
        """Write the credential now; best-effort, never raises into the caller."""
        if not self.path:
            return
        try:
            self.save(os.path.dirname(self.path))
        except Exception as exc:
            log("account %s: model window save failed: %s" % (self.uid, exc))

    def throttle_wait(self, model=None):
        """Seconds until this account can serve `model` again (0 = right now)."""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        with self._throttle_lock:
            wait = max(0.0, self.cooldown_until - now,
                       self.balance_until - now, self.breaker_until - now,
                       self.degrade_until - now)
            if model:
                wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None):
        changed = False
        with self._throttle_lock:
            if model:
                changed = self.model_cooldowns.pop(model, None) is not None
            else:
                changed = bool(self.model_cooldowns)
                self.model_cooldowns.clear()
            if self.last_error or self.cooldown_until or self.balance_until:
                self.last_error = ""
                self.cooldown_until = 0
                self.balance_until = 0.0
        if changed:
            self._persist_model_windows()

