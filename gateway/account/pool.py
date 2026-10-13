"""Account pool: selection, dispatch guards and persistence."""

import json
import os
import threading
import time
import urllib.error
import urllib.request

from gateway import settings as wb_settings
from gateway.account import jwt as _jwt
from gateway.account.affinity import SessionAffinity
from gateway.account.credentials import credential_to_kwargs, web_origin_for
from gateway.account.model import Account
from gateway.account.jwt import (AUTH_STATE_PATH, AUTH_TOKEN_PATH, LOGIN_ACCOUNT_PATH,
                                 LOGIN_PENDING, LOGIN_TTL_SECONDS, get_realm_config,
                                 jwt_exp, jwt_uid, normalize_epoch)
from gateway.account import transport as _transport
from gateway.common.log import log


class AccountPool(object):
    def __init__(self, directory, log=None):
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        # Smooth weighted round-robin state for the expiring-credits window,
        # keyed by uid (see _weighted_pick). Runtime-only: a restart just
        # restarts the rotation.
        self._expiry_weights = {}
        self.affinity = SessionAffinity()
        self.loaded_at = 0.0

    def load(self):
        """Rescan accounts/ while keeping in-memory Account objects alive.

        Replacing a live Account would swap its _refresh_lock/_save_lock for
        new ones: a hot reload landing mid-refresh lets the old and new
        objects refresh concurrently, each blind to the other's locks — and
        because the upstream rotates the refresh token, a stale writer can
        persist a now-dead token and brick the account. So for uids we
        already hold, only credential/display fields are refreshed in place;
        runtime state (locks, model cooldowns, product, last_error) survives.
        """
        fresh = {}
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
                    fresh[account.uid] = account
        with self._lock:
            old = {a.uid: a for a in self.accounts}
            merged = []
            for uid, new in fresh.items():
                kept = old.get(uid)
                if kept is None:
                    merged.append(new)
                    continue
                for field in ("enabled", "nickname", "domain", "realm",
                              "enterprise_id", "proxy", "credits",
                              "last_checkin", "path"):
                    setattr(kept, field, getattr(new, field))
                # Token fields: never roll a fresher in-memory credential
                # back to an older file (a refresh whose save() raced the
                # rescan must not be undone by the stale copy on disk).
                if (new.expires_at or 0) >= (kept.expires_at or 0):
                    kept.access_token = new.access_token
                    kept.refresh_token = new.refresh_token or kept.refresh_token
                    kept.expires_at = new.expires_at
                if new.last_daily_chat and not kept.last_daily_chat:
                    kept.last_daily_chat = new.last_daily_chat
                merged.append(kept)
            self.accounts = merged
            self.loaded_at = time.time()
            self.apply_reserve_credits()
            self.apply_expiring_window()
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

    def resolve(self, needle):
        """Find one account by uid, uid prefix or nickname.

        Returns (account, None) / (None, error). Exact uid wins; otherwise a
        unique prefix or nickname match is required, so an ambiguous needle
        never silently picks one.
        """
        needle = str(needle or "").strip()
        if not needle:
            return None, "empty account reference"
        with self._lock:
            accounts = list(self.accounts)
        for account in accounts:
            if account.uid == needle:
                return account, None
        prefix = [a for a in accounts if a.uid.startswith(needle)]
        if len(prefix) == 1:
            return prefix[0], None
        if len(prefix) > 1:
            return None, "ambiguous uid prefix %r: %s" % (
                needle, ", ".join(a.uid[:8] for a in prefix))
        named = [a for a in accounts if a.nickname == needle]
        if len(named) == 1:
            return named[0], None
        if len(named) > 1:
            return None, "ambiguous nickname %r: %s" % (
                needle, ", ".join(a.uid[:8] for a in named))
        return None, "no account matches %r" % needle

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
                if not account.last_daily_chat and existing.last_daily_chat:
                    account.last_daily_chat = existing.last_daily_chat
                if not account.proxy and existing.proxy:
                    account.proxy = existing.proxy
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            self.apply_reserve_credits()
            return account

    def apply_reserve_credits(self, value=None):
        """Re-resolve the low-credit guard for every account.

        settings.json is the source of truth and the per-account value is
        derived here, so the request path needs no extra settings lookup.
        """
        if value is None:
            value = wb_settings.reserve_credits(self.dir)
        try:
            value = max(0, int(value or 0))
        except (TypeError, ValueError):
            value = 0
        with self._lock:
            for account in self.accounts:
                account.reserve_credits = value
        return value

    def apply_expiring_window(self, values=None):
        """Re-resolve the expiring-credits window for every account.

        Same shape as apply_reserve_credits(): settings.json holds the window
        per realm (global by default) and each account reads its own, so the
        request path needs no extra settings lookup. The window is a dispatch
        preference, not a guard: it never makes an account unavailable, it
        only decides who is handed out first.
        """
        if values is None:
            values = wb_settings.expiring_window_days(self.dir)
        with self._lock:
            for account in self.accounts:
                account.expiring_window_days = int(values or 0)
        return values

    def apply_daily_token_limit(self, value=None, usage=None):
        """Re-resolve the daily token guard for every account.

        Same shape as apply_reserve_credits(): settings.json holds the limit,
        while `usage` (uid -> tokens counted today) comes from the caller,
        because only the upstream layer reads the usage log. Passing None
        keeps the last known counts, so a settings change never turns them
        into "unknown".
        """
        if value is None:
            value = wb_settings.daily_token_limit(self.dir)
        try:
            value = max(0, int(value or 0))
        except (TypeError, ValueError):
            value = 0
        with self._lock:
            for account in self.accounts:
                was_blocked = account.daily_limit_blocked()
                account.daily_token_limit = value
                if usage is not None:
                    try:
                        account.daily_tokens_today = int(usage.get(account.uid, 0))
                    except (TypeError, ValueError):
                        account.daily_tokens_today = None
                now_blocked = account.daily_limit_blocked()
                if now_blocked != was_blocked:
                    if now_blocked:
                        self.log("account %s parked: daily token limit reached "
                                 "(%s/%s tokens today)"
                                 % (str(account.uid)[:8],
                                    account.daily_tokens_today, value))
                    else:
                        self.log("account %s resumed: daily token limit cleared"
                                 % str(account.uid)[:8])
        return value

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
        any source (browser devtools, the hub's accounts/*.json, a desktop
        .info file via import_credential) and it is decoded, realm-detected
        and saved. Returns the Account.
        """
        kwargs = credential_to_kwargs(access_token, realm=realm)
        kwargs["refreshToken"] = str(refresh_token or "") or kwargs["refreshToken"]
        if nickname:
            kwargs["nickname"] = nickname
        kwargs["enabled"] = bool(enabled)
        account = Account(kwargs)
        self.add(account)
        return account

    def import_credential(self, blob, realm=None):
        """Register an account from desktop/hub credential JSON (see
        credential_to_kwargs). Returns the Account."""
        account = Account(credential_to_kwargs(blob, realm=realm))
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
        """Pure probe: never triggers a network refresh (health/list/loop
        budget all call this; only pick()/pick_for_session may refresh)."""
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot
                   if a.enabled and a.access_token
                   and a.ready(model=model, allow_refresh=False))

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

    def _pick_expiring_first(self, snapshot, exclude, model):
        """Serve the in-window accounts first, or None when none can.

        Within the window the pick is a weighted round-robin: an account's
        share grows the closer its credits are to lapsing, but no account is
        ever handed the whole window. A strict soonest-first order would put
        every request on the one account expiring tomorrow until the upstream
        rate-limited it, which is exactly the burst this spreads out.
        Accounts that lapse on the same day carry the same weight, so they
        keep taking turns (hub #174).
        """
        urgent = [a for a in snapshot if getattr(a, "in_expiring_window", None)
                  and a.in_expiring_window()]
        if not urgent:
            return None
        ready = [a for a in urgent if a.uid not in exclude and a.ready(model=model)]
        if not ready:
            return None
        return self._weighted_pick(ready)

    def _expiry_weight(self, account):
        """How much of the window's traffic this account should take.

        Linear in how close the credits are to lapsing, floored at 1 so an
        account at the far edge of the window still gets served rather than
        starved until the others run dry.
        """
        try:
            window = int(account.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        days = account.soonest_expiring_days()
        if days is None:
            return 1
        return max(1, int(round(window - days)) + 1)

    def _weighted_pick(self, ready):
        """Smooth weighted round-robin over the ready accounts.

        nginx's algorithm: every account accrues its weight each pick, the
        largest running total wins, and the winner pays back the whole
        round's weight. Equal weights degenerate to a plain rotation.
        """
        ready_uids = set(a.uid for a in ready)
        weights = [(account, self._expiry_weight(account)) for account in ready]
        with self._lock:
            state = dict((uid, value) for uid, value in self._expiry_weights.items()
                         if uid in ready_uids)
            total = 0
            chosen = None
            for account, weight in weights:
                state[account.uid] = state.get(account.uid, 0) + weight
                total += weight
                if chosen is None or state[account.uid] > state[chosen.uid]:
                    chosen = account
            if chosen is None:
                return None
            state[chosen.uid] -= total
            self._expiry_weights = state
        return chosen

    def pick(self, realm=None, exclude=None, model=None):
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        # Expiring-credits preference (#174): accounts whose soonest-expiring
        # package is inside the window are served first, so credits about to
        # lapse get spent. It only reorders who is offered; an account in the
        # window is still skipped when it is cooling down, parked, or
        # excluded. When the window covers nobody - or every covered account
        # is unavailable - the pool falls back to the plain round-robin it
        # always had, so a single lapsed account can never wedge the realm.
        account = self._pick_expiring_first(snapshot, exclude, model)
        if account is not None:
            return account
        with self._lock:
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
        payload = _transport.http_json(url, data=b"{}", method="POST", headers=headers,
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
            payload = _transport.http_json(url, method="GET", headers=headers, timeout=30, retries=2)
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
