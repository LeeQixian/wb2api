"""WorkBuddy account pool: credentials, OAuth device login, rotation.

Ported from workbuddy2api-hub/wb_accounts.py, slimmed for a single-user
local gateway: no Windows desktop-credential scanning, no dashboard export
rows, no named proxy slots (a per-account `proxy` URL still works).

Token files are written 0600 inside a 0700 directory - they are plain-text
credentials, treat the accounts dir like an SSH key directory.
"""
import re
import base64
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from gateway.fingerprint import derive_id, generate_request_id
from gateway import identity


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()


def opener_for_proxy(proxy):
    """Build (and cache) a urllib opener bound to one outbound proxy.

    Empty/blank means "direct", signalled by None so callers can fall back to
    the process default opener. One opener per account keeps each account's
    traffic pinned to its own exit IP instead of sharing a rotating pool.
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            _OPENER_CACHE[proxy] = opener
        return opener


def urlopen(req, timeout=30, proxy=""):
    """urlopen honouring an optional per-account proxy."""
    opener = opener_for_proxy(proxy)
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None, proxy=""):
    """urlopen + json decode with retries.

    Chinese networks and CDN edges routinely drop a TLS handshake with
    "SSL: UNEXPECTED_EOF_WHILE_READING"; a single retry almost always
    succeeds, so every upstream call goes through here.
    """
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=headers or {},
        )
        try:
            with urlopen(req, timeout=timeout, proxy=proxy) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last


REALM_CONFIGS = {
    "intl": {
        "name": "国际版 (Global)",
        "chat_upstream": "https://www.workbuddy.ai",
        "billing_upstream": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        "chat_ua": "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2",
        "billing_ua": "WorkBuddy/5.5.2",
        "has_checkin": False,
    },
    "cn": {
        "name": "国内版 (China)",
        "chat_upstream": "https://copilot.tencent.com",
        "billing_upstream": "https://www.codebuddy.cn",
        "origin": "https://www.codebuddy.cn",
        "domain": "copilot.tencent.com",
        "chat_ua": "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1",
        "billing_ua": "WorkBuddy/5.5.6",
        "has_checkin": True,
    },
}

AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
GET_RESOURCE_PATH = "/v2/billing/meter/get-user-resource"

LOGIN_PENDING = 11217
LOGIN_TTL_SECONDS = 600
USER_AGENT = REALM_CONFIGS['intl']['chat_ua']


def get_realm_config(realm):
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["intl"]


def _jwt_claims(token):
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
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1e11:
        number /= 1000.0
    return int(number)


def detect_realm_from_token(token, domain=None):
    iss = jwt_issuer(token).lower()
    dom = str(domain or "").lower()
    if "copilot.tencent.com" in dom or "codebuddy.cn" in dom \
            or "copilot.tencent.com" in iss or "codebuddy.cn" in iss:
        return "cn"
    return "intl"


class Account(object):
    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        token = str(data.get("accessToken") or "")
        self.uid = str(data.get("uid") or jwt_uid(token))
        self.nickname = str(data.get("nickname") or "")
        self.domain = str(data.get("domain") or "")
        self.realm = str(data.get("realm") or detect_realm_from_token(token, self.domain))
        if not self.domain:
            self.domain = get_realm_config(self.realm)["domain"]
        self.platform = str(data.get("platform") or "CLI")
        # 出站身分一律以 cli 開局：這是「預設 cli」的實際落點。
        # 手動/429 自動切換只影響這次執行；重啟就回到 cli（与源项目一致）。
        self.product = identity.PRODUCT_CLI
        self.saved_product = identity.normalize_product(data.get("product"))
        self.enterprise_id = str(data.get("enterpriseId") or "")
        self.access_token = token
        self.refresh_token = str(data.get("refreshToken") or "")
        self.expires_at = normalize_epoch(data.get("expiresAt")) or jwt_exp(token)
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source") or "oauth")
        self.proxy = str(data.get("proxy") or "").strip()
        self.enabled = data.get("enabled", True)
        self.last_error = str(data.get("lastError") or "")
        self.cooldown_until = float(data.get("cooldownUntil") or 0)
        # Per-model throttling. Upstream rate limits (code 6004 "usage exceeds
        # frequency limit") apply to ONE model for one account, not to the whole
        # account: other models keep working. Deliberately runtime-only.
        self.model_cooldowns = {}
        self.credits = data.get("credits") or None
        self.last_checkin = data.get("lastCheckin") or None
        # Serialise token refresh and file writes: request threads and the
        # scheduler can reach refresh()/save() for the same account at once;
        # upstream rotates the refresh token, so the last writer would win and
        # a freshly minted token could be overwritten by a stale snapshot.
        self._refresh_lock = threading.Lock()
        self._save_lock = threading.Lock()

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
        }

    def public(self):
        exp = self.expires_at or jwt_exp(self.access_token)
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
            "lastError": self.last_error,
            "inCooldown": self.cooldown_until > time.time(),
            "cooldownFor": round(max(0.0, self.cooldown_until - time.time())) or None,
            "credits": self.credits,
            "lastCheckin": self.last_checkin,
            "canCheckin": self.realm == "cn",
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
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
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

    def ready(self, model=None):
        if not self.enabled or not self.access_token:
            return False
        if self.cooldown_until > time.time():
            return False
        if model and self.model_cooldowns.get(model, 0.0) > time.time():
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
        # stale credential and came back 401/403.
        return self.refresh()

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
            self.last_error = "no refresh token; sign in again"
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
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=30,
                                proxy=self.proxy)
        except Exception as exc:
            self.last_error = "refresh failed: %s" % exc
            return False
        data = (payload.get("data") or {})
        data = data.get("data") or data
        token = data.get("accessToken")
        if not token:
            self.last_error = "refresh returned no token (%s)" % payload.get("msg")
            return False
        self.access_token = token
        self.refresh_token = data.get("refreshToken") or self.refresh_token
        self.expires_at = jwt_exp(token) or self.expires_at
        self.last_error = ""
        self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return True

    def can_checkin(self):
        if self.realm != "cn":
            return False
        if not self.last_checkin:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_checkin).startswith(today_str)

    def checkin(self):
        if self.realm != "cn":
            return {"ok": False, "error": "checkin is only available for CN realm accounts"}
        cfg = get_realm_config("cn")
        url = cfg["billing_upstream"] + CHECKIN_PATH
        headers = self.headers(purpose="billing")
        try:
            payload = http_json(url, data=b"{}", method="POST", headers=headers, timeout=15,
                                proxy=self.proxy)
            code = payload.get("code", -1)
            msg = payload.get("msg") or "ok"
            self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
            return {"ok": (code == 0 or code == 10001), "code": code, "msg": msg,
                    "data": payload.get("data")}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
                return {"ok": False, "error": body.get("msg") or ("HTTP %d" % exc.code)}
            except Exception:
                return {"ok": False, "error": "HTTP %d" % exc.code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def fetch_credits(self):
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
            res = http_json(url, data=json.dumps(body).encode(), method="POST", headers=headers,
                            timeout=30, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        data = res.get("data", {}).get("Response", {}).get("Data", {})
        accounts = data.get("Accounts") or []
        tot_remain, tot_used, tot_size = 0, 0, 0
        packages = []
        for a in accounts:
            pkg_name = a.get("PackageName") or "Package"
            if a.get("CycleCapacitySize", 0) > 0:
                remain = a.get("CycleCapacityRemain", 0)
                size = a.get("CycleCapacitySize", 0)
                used = max(0, size - remain)
                if a.get("CycleCapacityUsed", 0) > used:
                    used = a["CycleCapacityUsed"]
                    remain = max(0, size - used)
            else:
                remain = a.get("CapacityRemain", 0)
                used = a.get("CapacityUsed", 0)
                size = a.get("CapacitySize", 0)
            tot_remain += remain
            tot_used += used
            tot_size += size
            packages.append({"name": pkg_name, "remain": remain, "used": used, "size": size})
        self.credits = {
            "remain": tot_remain,
            "used": tot_used,
            "size": tot_size,
            "packages": packages,
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None):
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
        wait = max(0.0, self.cooldown_until - now)
        if model:
            wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None):
        if model:
            self.model_cooldowns.pop(model, None)
        else:
            self.model_cooldowns.clear()
        if self.last_error or self.cooldown_until:
            self.last_error = ""
            self.cooldown_until = 0


def _human_delta(seconds):
    if seconds is None:
        return None
    if seconds <= 0:
        return "expired"
    days = seconds / 86400.0
    if days >= 1:
        return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1:
        return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)


class SessionAffinity(object):
    def __init__(self, ttl=7200, max_entries=5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self.bindings = {}
        self._lock = threading.Lock()

    def get(self, key):
        if not key:
            return None
        with self._lock:
            entry = self.bindings.get(key)
            if not entry:
                return None
            uid, exp = entry
            if time.time() > exp:
                self.bindings.pop(key, None)
                return None
            self.bindings[key] = (uid, time.time() + self.ttl)
            return uid

    def bind(self, key, uid):
        if not key or not uid:
            return
        with self._lock:
            if len(self.bindings) >= self.max_entries:
                now = time.time()
                self.bindings = {k: v for k, v in self.bindings.items() if v[1] > now}
            self.bindings[key] = (uid, time.time() + self.ttl)

    def unbind(self, key):
        if not key:
            return
        with self._lock:
            self.bindings.pop(key, None)


class AccountPool(object):
    def __init__(self, directory, log=None):
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        self.affinity = SessionAffinity()
        self.loaded_at = 0.0

    def load(self):
        with self._lock:
            self.accounts = []
            if os.path.isdir(self.dir):
                for name in sorted(os.listdir(self.dir)):
                    if not name.endswith(".json"):
                        continue
                    path = os.path.join(self.dir, name)
                    try:
                        with open(path, encoding="utf-8") as fh:
                            account = Account(json.load(fh), path)
                    except Exception as exc:
                        self.log("account %s unreadable: %s" % (name, exc))
                        continue
                    if account.uid:
                        self.accounts.append(account)
            self.loaded_at = time.time()
            return self.accounts

    def list_public(self, realm=None):
        with self._lock:
            accs = self.accounts if (not realm or realm == "all") else \
                [a for a in self.accounts if a.realm == realm]
            return [a.public() for a in accs]

    def get(self, uid):
        with self._lock:
            for account in self.accounts:
                if account.uid == uid:
                    return account
        return None

    def add(self, account):
        with self._lock:
            existing = self.get(account.uid)
            if existing is not None:
                account.added_at = existing.added_at
                account.path = existing.path
                if not account.credits and existing.credits:
                    account.credits = existing.credits
                if not account.last_checkin and existing.last_checkin:
                    account.last_checkin = existing.last_checkin
                if not account.proxy and existing.proxy:
                    account.proxy = existing.proxy
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            return account

    def remove(self, uid):
        with self._lock:
            account = self.get(uid)
            if account is None:
                return False
            account.delete()
            self.accounts.remove(account)
            return True

    def import_token(self, access_token, refresh_token=None, realm=None,
                     nickname="", enabled=True):
        """Register (or replace) an account from a raw accessToken.

        The replacement for the dashboard's import flow: paste a token from
        any source (browser devtools, the hub's accounts/*.json) and it is
        decoded, realm-detected and saved. Returns the Account.
        """
        token = str(access_token or "").strip()
        if not token or token.count(".") < 2:
            raise ValueError("not a JWT access token")
        uid = jwt_uid(token)
        if not uid:
            raise ValueError("token has no sub claim; cannot key the account")
        detected = realm or detect_realm_from_token(token)
        cfg = get_realm_config(detected)
        account = Account({
            "uid": uid,
            "nickname": nickname or uid[:8],
            "domain": cfg["domain"],
            "realm": detected,
            "platform": "CLI",
            "accessToken": token,
            "refreshToken": refresh_token or "",
            "expiresAt": jwt_exp(token),
            "source": "import",
            "enabled": bool(enabled),
        })
        self.add(account)
        return account

    def set_enabled(self, uid, enabled):
        account = self.get(uid)
        if account is None:
            return None
        account.enabled = bool(enabled)
        if enabled:
            account.clear_error()
        account.save(self.dir)
        return account.public()

    def set_all_enabled(self, enabled, realm=None):
        with self._lock:
            for account in self.accounts:
                if realm and account.realm != realm:
                    continue
                account.enabled = bool(enabled)
                if enabled:
                    account.clear_error()
                account.save(self.dir)

    def count_ready(self, realm=None, model=None):
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot if a.enabled and a.access_token and a.ready(model=model))

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None):
        exclude = exclude or set()
        if session_key:
            bound_uid = self.affinity.get(session_key)
            if bound_uid and bound_uid not in exclude:
                account = self.get(bound_uid)
                if account and account.realm == realm and account.ready(model=model):
                    return account
                self.affinity.unbind(session_key)
        account = self.pick(realm=realm, exclude=exclude, model=model)
        if account and session_key:
            self.affinity.bind(session_key, account.uid)
        return account

    def pick(self, realm=None, exclude=None, model=None):
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
            start = self._cursor
        total = len(snapshot)
        if total == 0:
            return None
        for offset in range(total):
            index = (start + offset) % total
            account = snapshot[index]
            if account.uid in exclude:
                continue
            if account.ready(model=model):
                with self._lock:
                    self._cursor = (index + 1) % total
                return account
        return None

    def representative(self, realm=None):
        with self._lock:
            candidates = [a for a in self.accounts if not realm or a.realm == realm]
            for account in candidates:
                if account.access_token:
                    return account
            return candidates[0] if candidates else None

    def start_login(self, realm="intl", platform="CLI"):
        cfg = get_realm_config(realm)
        url = "%s%s?platform=%s" % (cfg["chat_upstream"], AUTH_STATE_PATH,
                                    urllib.parse.quote(str(platform)))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
        }
        payload = http_json(url, data=b"{}", method="POST", headers=headers,
                            timeout=30, retries=3, log=self.log)
        data = payload.get("data") or {}
        state = data.get("state")
        auth_url = data.get("authUrl")
        if not state or not auth_url:
            raise RuntimeError("auth/state returned no state/authUrl: %s" % payload)
        with self._lock:
            self.logins[state] = {"created": time.time(), "platform": platform, "realm": realm}
        return {"state": state, "authUrl": auth_url, "realm": realm, "platform": platform}

    def poll_login(self, state):
        state = str(state or "").strip()
        with self._lock:
            info = self.logins.get(state)
        if not info:
            return {"status": "unknown", "message": "state not recognised - start the login again"}
        if time.time() - info["created"] > LOGIN_TTL_SECONDS:
            with self._lock:
                self.logins.pop(state, None)
            return {"status": "expired", "message": "login window expired - start again"}
        realm = info.get("realm") or "intl"
        cfg = get_realm_config(realm)
        url = "%s%s?state=%s" % (cfg["chat_upstream"], AUTH_TOKEN_PATH, urllib.parse.quote(state))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": cfg["billing_ua"],
            "Origin": cfg["origin"],
            "Referer": cfg["origin"] + "/",
        }
        try:
            payload = http_json(url, method="GET", headers=headers, timeout=30, retries=2)
        except Exception as exc:
            return {"status": "pending", "message": "poll error: %s" % exc}
        code = payload.get("code")
        if code == LOGIN_PENDING:
            return {"status": "pending", "message": payload.get("msg") or "waiting for browser login"}
        if code != 0:
            return {"status": "error", "message": "code=%s msg=%s" % (code, payload.get("msg"))}
        data = payload.get("data") or {}
        token = data.get("accessToken")
        if not token:
            return {"status": "pending", "message": "waiting for token"}
        uid = jwt_uid(token)
        nickname = ""
        try:
            acct_url = "%s%s?state=%s" % (cfg["chat_upstream"], LOGIN_ACCOUNT_PATH,
                                          urllib.parse.quote(state))
            acct_headers = dict(headers)
            acct_headers["Authorization"] = "Bearer " + token
            req_acct = urllib.request.Request(acct_url, method="GET", headers=acct_headers)
            with urllib.request.urlopen(req_acct, timeout=15) as resp_acct:
                profile = json.loads(resp_acct.read().decode("utf-8"))
                profile_data = profile.get("data") or {}
                nickname = str(profile_data.get("nickname") or "")
        except Exception:
            pass
        account = Account({
            "uid": uid,
            "nickname": nickname or uid[:8],
            "domain": data.get("domain") or cfg["domain"],
            "realm": realm,
            "platform": info["platform"],
            "accessToken": token,
            "refreshToken": data.get("refreshToken") or "",
            "expiresAt": normalize_epoch(data.get("expiresAt")) or jwt_exp(token),
            "source": "oauth",
            "enabled": True,
        })
        self.add(account)
        if realm == "cn":
            try:
                account.checkin()
            except Exception:
                pass
        with self._lock:
            self.logins.pop(state, None)
        return {"status": "ok", "account": account.public()}

    def cancel_login(self, state):
        with self._lock:
            return self.logins.pop(state, None) is not None
