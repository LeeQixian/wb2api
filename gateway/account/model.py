"""Account model: credentials, outbound headers, cooldowns and credit state."""

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from gateway.fingerprint import derive_id, generate_request_id
from gateway import identity
from gateway import settings as wb_settings
from gateway.account import jwt as _jwt
from gateway.account import backoff as _backoff
from gateway.account.affinity import _human_delta
from gateway.account.backoff import (BREAKER_THRESHOLD, DEGRADE_THRESHOLD,
                                     breaker_backoff, degrade_backoff,
                                     next_local_4am, soft_backoff,
                                     unverified_soft_backoff)
from gateway.account.credentials import deduction_end_text
from gateway.account.jwt import (EXPIRY_SENTINEL_DAYS, GET_RESOURCE_PATH,
                                 REFRESH_PATH, detect_realm_from_token,
                                 domain_for_realm, get_realm_config, jwt_exp,
                                 jwt_uid, normalize_epoch)
from gateway.account.maintenance import MaintenanceMixin
from gateway.account import transport as _transport
from gateway.common.log import log


#: rewritten from scratch each run; never persisted (hub #144).
RUNTIME_ONLY_FIELDS = ("lastError", "cooldownUntil")

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
        # The CN desktop build stores its nickname as an encrypted envelope
        # ({"$wbEncrypted": ...}) rather than plain text. Stringifying that would
        # paint an entire dictionary into the account row, so anything that is not
        # a plain string is dropped and the uid prefix is shown instead.
        raw_nickname = data.get("nickname")
        self.nickname = (raw_nickname.strip()
                         if isinstance(raw_nickname, str) and "$wbEncrypted" not in raw_nickname
                         else "")
        self.domain = str(data.get("domain") or "")
        self.realm = str(data.get("realm") or detect_realm_from_token(token, self.domain))
        # #94: a stale domain that names the OTHER realm would drag X-Domain
        # back to the wrong exit; align it with the token-resolved realm.
        self.domain = domain_for_realm(self.realm, self.domain)
        self.platform = str(data.get("platform") or "CLI")
        # Read the outbound identity back from the credential file: manual and
        # 429 switches both persist through save(), so a restart resumes the
        # last path; a missing or invalid value falls back to the desktop one.
        self.product = identity.normalize_product(data.get("product"))
        self.enterprise_id = str(data.get("enterpriseId") or "")
        self.access_token = token
        self.refresh_token = str(data.get("refreshToken") or "")
        self.expires_at = normalize_epoch(data.get("expiresAt")) or jwt_exp(token)
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source") or "oauth")
        self.proxy = str(data.get("proxy") or "").strip()
        self.enabled = data.get("enabled", True)
        # Live error state, never restored from the credential file (hub #144):
        # the label describes the last upstream answer, not the credential. It
        # used to be persisted and read back, so a 429 that had long since
        # recovered came back after every restart - a successful request only
        # clears it in memory, and the file is rewritten on unrelated events,
        # so a stale value could sit on disk for days. Runtime-only, like
        # model_cooldowns below; save() strips RUNTIME_ONLY_FIELDS.
        self.last_error = ""
        self.cooldown_until = 0.0
        # Per-model throttling. Upstream rate limits (code 6004 "usage exceeds
        # frequency limit") apply to ONE model for one account, not to the whole
        # account: other models keep working. Deliberately runtime-only.
        self.model_cooldowns = {}
        self.credits = data.get("credits") or None
        self.last_checkin = data.get("lastCheckin") or None
        self.last_daily_chat = data.get("lastDailyChat") or None
        # Low-credit guard: once the balance reaches this level the account
        # stops being handed out, so it never drops to zero (a zero balance is
        # what makes the upstream start sending nagging SMS). Resolved from the
        # global setting by AccountPool.apply_reserve_credits(); 0 disables it.
        self.reserve_credits = 0
        # Daily token guard: an account that already burned this many tokens
        # today stops being handed out, so a client that would burn the rest
        # of the day's quota rotates to another account instead of hitting
        # the upstream wall. Resolved from the global setting by
        # AccountPool.apply_daily_token_limit(); 0 disables it.
        # daily_tokens_today stays None until the usage log has been folded
        # at least once, so a fresh process never parks anyone on an unknown
        # count.
        self.daily_token_limit = 0
        self.daily_tokens_today = None
        # 402 (out of credits): a hard park until the next local 04:00 (#157),
        # instead of a short cooldown that would retry an empty account all
        # day. Runtime-only, like the other throttle windows; a balance
        # refresh that shows credits again lifts it early.
        self.balance_until = 0.0
        # Expiring-credits preference (#174): while this account's soonest-
        # expiring credit package is inside this many days, the pool hands the
        # account out before accounts whose credits lapse later, so credits
        # about to be written off get spent first. Resolved from the global
        # setting by AccountPool.apply_expiring_window(); 0 disables it.
        self.expiring_window_days = 0
        # Account-level failure streaks (#163): soft rate-limit streak, hard
        # failure counter (breaker) and unknown-failure counter (degrade).
        # All runtime-only: a restart clears them and the account gets a
        # clean slate.
        self.soft_streak = 0
        # Unscoped-429 counter, deliberately separate from soft_streak so the
        # two ladders never feed each other (hub 72e51b4).
        self.unscoped_streak = 0
        self.fails = 0
        self.degrade_count = 0
        self.breaker_until = 0.0
        self.degrade_until = 0.0
        # Serialise token refresh and file writes: request threads and the
        # scheduler can reach refresh()/save() for the same account at once;
        # upstream rotates the refresh token, so the last writer would win and
        # a freshly minted token could be overwritten by a stale snapshot.
        self._refresh_lock = threading.Lock()
        self._save_lock = threading.Lock()
        # Health/list snapshots read this state while request threads update
        # it. Keep it separate from _refresh_lock, which spans network requests.
        self._throttle_lock = threading.Lock()
        # Guards fetch_credits(): the background refresher (#174) and the
        # checkin / daily-activity paths all fetch the balance; two overlapping
        # calls would each spend an upstream billing request and then race to
        # publish whichever answer came back second.
        self._credits_lock = threading.Lock()

    def to_dict(self):
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
        """True when the low-credit guard should keep this account idle.

        Only a *known* balance can block: an account whose credits were never
        fetched stays usable, otherwise a fresh install would look empty.
        """
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

    def daily_limit_blocked(self):
        """True when today's counted usage has reached the configured limit.

        Only a *counted* day can block: until the proxy has folded the usage
        log once, the count is None and the account stays usable.
        """
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
            "sessionId": derive_id(self.uid, "session"),
        }

    def save(self, directory):
        # Private credential: 0700 dir, 0600 file. os.open honours the mode at
        # creation so there is no window where the token is group/world read.
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
                # Live error/cooldown state stays out of the credential file;
                # writing it is what made a long-recovered 429 label reappear
                # on every restart (hub #144, see RUNTIME_ONLY_FIELDS).
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
        # Refresh is a last resort and its result decides availability.
        # Returning True unconditionally here kept handing out an account
        # whose token was about to expire, so requests went upstream with a
        # stale credential and came back 401/403. Read-only callers (health,
        # list, credits) pass allow_refresh=False: a 30s-timeout network call
        # must never hide behind a status probe.
        if not allow_refresh:
            return False
        return self.refresh()

    def unavailable_reason(self, model=None):
        """One-line reason this account cannot take work right now ('' if it can).

        Mirrors ready()'s order so the message names what actually blocked it
        (hub f398a3c); unlike ready() it never refreshes a stale token.
        """
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
            return "%s (%s left)" % (label, _human_delta(until - now) or "?")
        if self.reserve_blocked():
            return "balance below reserve"
        if self.daily_limit_blocked():
            return "daily token limit reached"
        exp = self.expires_at or jwt_exp(self.access_token)
        if exp and exp - now <= 120:
            return "credential expired"
        return ""

    def headers(self, purpose="chat"):
        """組出這一輪的出站標頭。

        chat 用途走 identity（CLI 頭 / WorkBuddy 頭，可切換）；
        billing 用途維持輕量標頭，計費端點不吃那套身分。
        """
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
                "X-Session-ID": derive_id(self.uid, "session"),
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
            conversation_id=getattr(self, "conversation_id", None),
            enterprise_id=self.enterprise_id,
            tenant_id=self.enterprise_id,
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "en-US" if self.realm == "intl" else "zh-CN",
            "X-Machine-ID": derive_id(self.uid, "machine"),
            "X-Session-ID": derive_id(self.uid, "session"),
        }
        headers.update(identity_headers)
        return headers

    def set_product(self, value):
        """切換出站身分（cli <-> workbuddy）。回傳 True 表示真的換了。"""
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
        """這個帳號目前身分該打的端點。"""
        return identity.endpoint_for(self.realm, self.product)[0]

    def refresh(self):
        with self._refresh_lock:
            return self._refresh_locked()

    def _refresh_locked(self):
        if not self.refresh_token:
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
            self._set_last_error("refresh failed: %s" % exc)
            return False
        data = (payload.get("data") or {})
        data = data.get("data") or data
        token = data.get("accessToken")
        if not token:
            self._set_last_error("refresh returned no token (%s)" % payload.get("msg"))
            return False
        self.access_token = token
        self.refresh_token = data.get("refreshToken") or self.refresh_token
        self.expires_at = jwt_exp(token) or self.expires_at
        with self._throttle_lock:
            self.last_error = ""
            self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return True

    def fetch_credits(self):
        """Refresh the balance; a refresh that shows credits again also
        lifts an early 402 park (#157). Serialised per account: the background
        refresher and the check-in / daily-activity paths all land here.
        """
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
            # one short attempt: /credits fans this out per account on a free
            # route; retries=3 x 30s would hold handler threads for minutes
            res = _transport.http_json(url, data=json.dumps(body).encode(), method="POST", headers=headers,
                            timeout=10, retries=1, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        data = res.get("data", {}).get("Response", {}).get("Data", {})
        accounts = data.get("Accounts") or []
        # #174: packages go through the hub's parser so they carry
        # days_left / expire_time / no_expiry; the expiring-credits dispatch
        # preference and the reserve guard both read those fields.
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
        # a refresh that shows credits again lifts an early 402 park (#157)
        self.revive_balance_cooldown()
        return {"ok": True, "credits": self.credits}

    def soonest_expiring_days(self):
        """Days until the earliest credit package that still has credits left.

        None when nothing can be said: no credit data, no package with a
        usable remainder, or an end date the upstream never gave.

        Packages the upstream never really expires are skipped. The enterprise
        quota resets its allowance rather than voiding it, and the free plan /
        trial packs carry a deduction deadline years out, so both are marked
        `no_expiry` where they are built. Treating either as an expiry would
        make those accounts look permanently "about to lapse" - the free plan
        would even look urgent at every month end, when its billing cycle rolls
        over but its credits keep working.
        """
        credits = self.credits
        if not isinstance(credits, dict):
            return None
        soonest = None
        for package in (credits.get("packages") or []):
            if not isinstance(package, dict):
                continue
            # `no_expiry` is the general rule; the package_code check is a
            # belt-and-braces guard for credit blobs written before the flag
            # existed, which are still on disk until the next refresh.
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
        """True when this account should jump the dispatch queue.

        Unknown or missing credit data is never a preference: an account the
        gateway cannot judge keeps its plain round-robin turn instead of being
        pushed to the front on a guess.
        """
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
        # Expiry follows DeductionEndTime, not CycleEndTime: the cycle end is
        # only a billing boundary. For free/trial packs the two differ by
        # years, so CycleEndTime would flag them expiring every month-end.
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
        """402 / out of credits: park until the next local 04:00.

        A short cooldown would hand the empty account out again minutes
        later, and every request to it fails the same way; the daily reset
        is when credits come back, so that is the wall clock this waits for.
        A refresh that shows credits again can lift it early - see
        revive_balance_cooldown().
        """
        until = next_local_4am()
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.balance_until = max(self.balance_until, until)
        return until

    def revive_balance_cooldown(self):
        """A balance refresh that shows credits again lifts the 402 park only.

        Rate-limit and model cooldowns stay exactly as they are; this is not
        a general clear_error().
        """
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
            self.soft_streak += 1
            wait = soft_backoff(self.soft_streak)
            self.last_error = str(message)[:200]
            self.cooldown_until = max(self.cooldown_until, time.time() + wait)
        return wait

    def note_unscoped_rate(self, model):
        """An unscoped soft 429: grow its own streak, return this model's window.

        Counting is deliberately separate from soft_streak: sharing one counter
        let the ladders feed each other (hub 72e51b4). A served request clears
        both through note_success().
        """
        with self._throttle_lock:
            self.unscoped_streak += 1
            return unverified_soft_backoff(self.unscoped_streak)

    def note_failure(self, message):
        """5xx / transport failure: feed the breaker counter."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.fails += 1
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                self.breaker_until = max(self.breaker_until, time.time() + wait)
        return self.breaker_until

    def note_unknown_failure(self, message):
        """Unknown error: degrade counter plus the shared breaker counter."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
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
        with self._throttle_lock:
            if model:
                self.model_cooldowns.pop(model, None)
            self.soft_streak = 0
            self.unscoped_streak = 0
            self.fails = 0
            self.degrade_count = 0
            self.breaker_until = 0.0
            self.degrade_until = 0.0
            self.last_error = ""
            self.last_error_detail = ""
            self.cooldown_until = 0.0

    def _set_last_error(self, message):
        """Record refresh errors alongside the state shown in the panel."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None):
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            if model:
                # Model-scoped throttle: keep the account usable for every other model.
                wait = max(1.0, float(until) - time.time()) if until else (
                    3.0 if single_account else float(cooldown))
                self.model_cooldowns[model] = time.time() + wait
                return
            actual_cooldown = 3 if single_account else cooldown
            self.cooldown_until = time.time() + actual_cooldown

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
        with self._throttle_lock:
            if model:
                self.model_cooldowns.pop(model, None)
            else:
                self.model_cooldowns.clear()
            if self.last_error or self.cooldown_until or self.balance_until:
                self.last_error = ""
                self.cooldown_until = 0
                self.balance_until = 0.0

