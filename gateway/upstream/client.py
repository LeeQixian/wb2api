"""Outbound request with the retry/rotation loop."""

import threading
from gateway import protocol as _protocol
from gateway.protocol.model_info import detect_model_realm
from gateway.protocol.request import (build_upstream_body, derive_affinity_key, inject_prompt_cache_key, prompt_cache_key_enabled)
from gateway.upstream.catalog import CHAT_PATH
from gateway.upstream.errors import ContentRejected, RateLimited
from gateway.upstream.guards import (apply_daily_token_limit, auto_switch_product_enabled, seconds_until_local_midnight)
from gateway.upstream.realm import get_realm
from gateway.upstream.retry import (AUTO_SWITCH_PRODUCT, MAX_PRODUCT_SWITCHES, RESTRICTED_403_COOLDOWN, credential_scope_phrase, is_transient, parse_rate_limit_reset, rate_limit_is_account_level, rate_limit_is_soft_shape, realm_model_throttled, reset_switch_counter, restriction_is_account_level, retry_after_seconds)
from gateway.upstream.timeouts import (_apply_stream_idle_timeout,
                                       upstream_timeouts)
import json
import os
import time
import urllib.error
import urllib.request
from gateway import accounts as wb_accounts
from gateway import settings as wb_settings
from gateway.upstream import state as _state
from gateway import translate as tr
from gateway.common.log import log
from gateway.common import paths
from gateway.upstream import retry, errors, timeouts


ACCOUNTS_DIR = os.environ.get("WB_ACCOUNTS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "accounts")
_state._POOL = None
_state._lock = threading.Lock()
AFFINITY_DEBUG = os.environ.get("WB_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")


def open_upstream(pool, payload, session_key=None, target_realm=None, log=log,
                  prebuilt_body=None):
    """Pick an account and open the upstream chat stream.

    Returns (response, account). Raises RateLimited / ContentRejected /
    urllib.error.HTTPError / RuntimeError per the classification above.
    Logic preserved line-for-line from wb_proxy.open_upstream; POOL and the
    logger became parameters.
    """
    # Refresh the daily token guard before picking. The scan underneath is
    # incremental and TTL-cached, so this is a stat() plus a cached dict on
    # the hot path, and an account parked by the guard is skipped like any
    # other unusable one.
    apply_daily_token_limit(pool)
    realm = target_realm or detect_model_realm(payload.get("model")) or get_realm()
    model = str(payload.get("model") or "")
    # reuse the caller's body when given: rebuilds are pure overhead (hub 8bc1f0d)
    upstream_body = (prebuilt_body if prebuilt_body is not None
                     else build_upstream_body(payload))
    # Derive a session key from the stable message prefix when the client
    # sends none; do it after build_upstream_body so the key matches what is
    # actually sent.
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
    # Read once per request, not per attempt: this is a settings read, and a
    # settings lookup on every retry would be pure overhead. The env override
    # keeps the historical switch working for headless installs.
    auto_switch = AUTO_SWITCH_PRODUCT or auto_switch_product_enabled(pool)
    max_attempts = max(2, total) + 1 + (MAX_PRODUCT_SWITCHES if auto_switch else 0)
    # Panel parity (#148): header/idle timeouts are read once per request,
    # never inside the retry loop; a duck-typed pool without a dir keeps the
    # hub defaults.
    header_timeout, idle_timeout = upstream_timeouts(getattr(pool, "dir", None))
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
            resp = wb_accounts.urlopen(req, timeout=header_timeout,
                                       proxy=account.proxy)
            _apply_stream_idle_timeout(resp, idle_timeout)
            # #163: a served request clears every account-level penalty
            # (soft streak, breaker, degrade) along with the model park.
            account.note_success(model=model)
            reset_switch_counter(account, model)
            return resp, account
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                reset_at = parse_rate_limit_reset(detail)
                if rate_limit_is_account_level(detail, reset_at):
                    wait = account.note_soft_rate("HTTP 429 (account soft rate)")
                    log("account %s soft-rate limited (%r), cooling %.0fs (streak %d)"
                        % (account.uid[:8], credential_scope_phrase(detail),
                           wait, account.soft_streak))
                    if session_key and pool:
                        pool.affinity.unbind(session_key)
                    last_error = exc
                    last_429 = exc
                    last_429_detail = detail
                    continue
                wait = max(1.0, reset_at - time.time()) if reset_at else 60.0
                # Unscoped soft shape without a reset clock: park one model on
                # the short ladder, never the credential ladder (hub 72e51b4).
                if reset_at is None and rate_limit_is_soft_shape(detail):
                    wait = account.note_unscoped_rate(model)
                    log("account %s: unscoped 429, parking '%s' for %.0fs (streak %d)"
                        % (account.uid[:8], model, wait,
                           getattr(account, "unscoped_streak", 0)))
                account.note_error("HTTP 429 (model throttled)", model=model,
                                   until=reset_at, cooldown=wait)
                if auto_switch and _try_switch_product(account, model):
            # A new identity means a new quota line: clear it from `tried`
            # and drop the model cooldown recorded a moment ago.
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
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                if restriction_is_account_level(detail):
                    # Park the restricted account and rotate; the content-
                    # review passthrough contract stays for every other 403.
                    account.note_error("HTTP 403 (account restricted)",
                                       cooldown=RESTRICTED_403_COOLDOWN,
                                       single_account=(total <= 1))
                    log("account %s restricted by upstream (403 11140), "
                        "cooling %.0fs" % (account.uid[:8],
                                            RESTRICTED_403_COOLDOWN))
                    if session_key and pool:
                        pool.affinity.unbind(session_key)
                    last_error = exc
                    last_403_detail = detail
                    continue
                log("upstream 403 for '%s' (content review), passing through" % model)
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                last_error = exc
                last_403_detail = detail
                break
            if exc.code == 402:
                try:
                    detail = exc.read(400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                account.note_balance_cooled(detail or "HTTP 402 (insufficient credits)")
                log("account %s out of credits (402), parked until 04:00"
                    % account.uid[:8])
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                last_error = exc
                continue
            if exc.code == 401:
                log("account %s rejected (HTTP 401), rotating" % account.uid[:8])
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                account.note_error("HTTP 401",
                                   cooldown=60,
                                   single_account=(total <= 1))
                last_error = exc
                continue
            if exc.code >= 500:
                transient_hits += 1
                account.note_failure("HTTP %d" % exc.code)
                log("upstream %s for '%s' on %s, retrying (fails=%d)"
                    % (exc.code, model, account.uid[:8], account.fails))
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
                log("upstream connection hiccup for '%s' on %s (%s), retrying"
                    % (model, account.uid[:8], type(exc).__name__))
                last_error = exc
                time.sleep(min(0.6 * transient_hits, 2.0))
                continue
            account.note_unknown_failure(str(exc)[:120])
            account.note_error(str(exc)[:120], cooldown=60, single_account=(total <= 1))
            log("upstream failure on %s for '%s': %s"
                % (account.uid[:8], model, str(exc)[:120]))
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
    enabled = [a for a in (pool.accounts if pool else [])
               if a.realm == realm and a.enabled and a.access_token]
    if enabled and all(a.daily_limit_blocked() for a in enabled):
        reason = ("every usable account reached today's token limit (%s per "
                  "account); the pool resumes after local midnight"
                  % wb_settings.daily_token_limit(pool.dir))
        raise RateLimited(None, reason,
                          wait=seconds_until_local_midnight(), message=reason)
    detail = ""
    pool_accounts = [a for a in (pool.accounts if pool else []) if a.realm == realm]
    if pool_accounts:
        reasons = []
        for a in pool_accounts:
            try:
                why = a.unavailable_reason(model=model)
            except Exception:
                why = ""
            if why:
                reasons.append("%s %s" % ((a.uid or "?")[:8], why))
        if reasons:
            detail = "; accounts: %s" % ", ".join(reasons)
        elif not any(a.enabled and a.access_token for a in pool_accounts):
            detail = "; accounts: all disabled"
    raise RuntimeError("no usable account for realm '%s': all are disabled, "
                       "cooling down, expired or parked by the daily token "
                       "limit%s" % (realm, detail))
