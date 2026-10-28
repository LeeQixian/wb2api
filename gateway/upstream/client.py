"""Outbound request with the retry/rotation loop."""

import threading
from gateway import protocol as _protocol
from gateway.protocol.model_info import detect_model_realm
from gateway.protocol.request import (build_upstream_body, derive_affinity_key, inject_prompt_cache_key, prompt_cache_key_enabled)
from gateway.upstream.catalog import CHAT_PATH
from gateway.upstream.errors import ContentRejected, RateLimited
from gateway.upstream.guards import (apply_daily_token_limit, auto_switch_product_enabled, seconds_until_local_midnight)
from gateway.upstream.realm import get_realm
from gateway.upstream.retry import (AUTO_SWITCH_PRODUCT, MAX_PRODUCT_SWITCHES, RESTRICTED_403_COOLDOWN, LocalNetworkError, credential_scope_phrase, exc_detail, is_transient, looks_like_local_network, parse_rate_limit_reset, rate_limit_is_account_level, rate_limit_is_soft_shape, realm_model_throttled, reset_switch_counter, restriction_is_account_level, retry_after_seconds)
from gateway.upstream.timeouts import (_apply_stream_idle_timeout,
                                       upstream_timeouts)
import json
import os
import time
import urllib.error
import urllib.request
from gateway import accounts as wb_accounts
from gateway import identity as wb_identity
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


# Per-account concurrency cap (account_max_concurrent, 0 = unlimited); upstream
# meters tokens per account, so overloading one earns a 429 and loses its cache.
_ACCOUNT_INFLIGHT = {}
_ACCOUNT_INFLIGHT_LOCK = threading.Lock()
ACCOUNT_SLOT_WAIT_MAX = 2.0


def account_slot_acquire(account, limit):
    """Take one in-flight slot for this account; True when it was free."""
    if limit <= 0:
        return True
    with _ACCOUNT_INFLIGHT_LOCK:
        current = _ACCOUNT_INFLIGHT.get(account.uid, 0)
        if current >= limit:
            return False
        _ACCOUNT_INFLIGHT[account.uid] = current + 1
        return True


def account_slot_release(account):
    """Return a slot; a no-op when the cap is off."""
    if account is None:
        return
    with _ACCOUNT_INFLIGHT_LOCK:
        current = _ACCOUNT_INFLIGHT.get(account.uid, 0)
        if current <= 1:
            _ACCOUNT_INFLIGHT.pop(account.uid, None)
        else:
            _ACCOUNT_INFLIGHT[account.uid] = current - 1


def bind_account_slot_to_response(response, account):
    """Release the slot when the upstream body is closed, not at header time."""
    if account is None:
        return
    if response is None:
        account_slot_release(account)
        return
    released = [False]

    def release_once():
        if not released[0]:
            released[0] = True
            account_slot_release(account)

    try:
        original_close = response.close
    except Exception:
        release_once()
        return

    def close_and_release(*args, **kwargs):
        try:
            return original_close(*args, **kwargs)
        finally:
            release_once()

    try:
        response.close = close_and_release
    except Exception:
        release_once()


def account_inflight_snapshot():
    """{uid: in-flight count} for diagnostics."""
    with _ACCOUNT_INFLIGHT_LOCK:
        return dict(_ACCOUNT_INFLIGHT)


def open_upstream(pool, payload, session_key=None, target_realm=None, log=log,
                  prebuilt_body=None, pin_account=None):
    """Pick an account and open the upstream chat stream; (response, account).
    Raises RateLimited / ContentRejected / HTTPError / RuntimeError."""
    apply_daily_token_limit(pool)
    realm = target_realm or detect_model_realm(payload.get("model")) or get_realm()
    model = str(payload.get("model") or "")
    # reuse the caller's body when given: rebuilds are pure overhead
    upstream_body = (prebuilt_body if prebuilt_body is not None
                     else build_upstream_body(payload))
    # Derive a session key from the stable message prefix when the client
    # sends none, after build_upstream_body so it matches what is sent.
    if not session_key:
        session_key = derive_affinity_key(upstream_body.get("messages"))
        if session_key and AFFINITY_DEBUG:
            log("affinity: derived %s for %d msgs"
                % (session_key, len(upstream_body.get("messages") or [])))
    # One user send reuses a single conversation request id across retries.
    turn_request_id = wb_identity.new_turn_request_id()
    total = max(1, pool.count_ready(realm, model=model)) if pool else 1
    tried = set()
    # A 401 can mean the access token died before its JWT clock did: refresh
    # once per account per request, then retry that same account.
    refreshed_uids = set()
    refresh_uid = None
    last_error = None
    last_uid = None
    last_429 = None
    last_429_detail = ""
    last_403_detail = ""
    transient_hits = 0
    # Any local-network failure this round flips the final error class;
    # the remedy differs completely (check the host, not the accounts).
    local_net_hits = 0
    # Read once per request, never per attempt: a settings lookup on every
    # retry would be pure overhead.
    auto_switch = AUTO_SWITCH_PRODUCT or auto_switch_product_enabled(pool)
    max_attempts = max(2, total) + 1 + (MAX_PRODUCT_SWITCHES if auto_switch else 0)
    # Read once per request, never inside the retry loop.
    header_timeout, idle_timeout = upstream_timeouts(getattr(pool, "dir", None))
    # A pinned account serves the first attempt only; later attempts rotate.
    pin_uid = pin_account.uid if pin_account is not None else None
    slot_limit = wb_settings.account_max_concurrent(
        getattr(pool, "dir", None) or _state.ACCOUNTS_DIR)
    # busy = "full right now", never a failure: it must not affect the breaker.
    busy = set()
    slot_held = None

    def pick_account():
        while True:
            candidate = pool.pick_for_session(realm=realm, session_key=session_key,
                                              exclude=tried | busy,
                                              model=model) if pool else None
            if candidate is None:
                return None
            if account_slot_acquire(candidate, slot_limit):
                return candidate
            busy.add(candidate.uid)
            if len(busy) >= max(1, total):
                return None

    def wait_slot(account):
        """A pinned account has no alternative: wait a bounded time for a slot."""
        if account is None:
            return None
        got = account_slot_acquire(account, slot_limit)
        waited = 0.0
        while not got and waited < ACCOUNT_SLOT_WAIT_MAX:
            time.sleep(0.05)
            waited += 0.05
            got = account_slot_acquire(account, slot_limit)
        return account if got else None

    for _attempt in range(max_attempts):
        # Any path that did not hand the response out gives the slot back here.
        if slot_held is not None:
            account_slot_release(slot_held)
            slot_held = None
        if refresh_uid is not None:
            # The post-refresh retry targets exactly the account just refreshed.
            account = wait_slot(pool.get(refresh_uid) if pool else None)
            refresh_uid = None
        elif pin_uid is not None:
            account = wait_slot((pool.get(pin_uid) if pool else None) or pin_account)
            pin_uid = None
        else:
            account = pick_account()
        if account is None:
            if (pin_account is None and (transient_hits or local_net_hits)
                    and _attempt < max_attempts - 1):
                # Rotation is bookkeeping, not an upstream attempt: re-pick in
                # this iteration so a small pool keeps its retry budget.
                tried.clear()
                time.sleep(min(1.5 * (transient_hits or local_net_hits), 3.0))
                account = pick_account()
            if account is None:
                break
        if account.realm != realm:
            account_slot_release(account)
            if session_key and pool:
                pool.affinity.unbind(session_key)
            continue
        tried.add(account.uid)
        last_uid = account.uid
        slot_held = account
        chat_url = account.chat_base_url() + CHAT_PATH
        if prompt_cache_key_enabled():
            attempt_body = inject_prompt_cache_key(upstream_body, account.uid, session_key)
        else:
            attempt_body = upstream_body
        product = getattr(account, "product", wb_identity.PRODUCT_DESKTOP)
        cli_identity = product == wb_identity.PRODUCT_CLI
        vscode_identity = product == wb_identity.PRODUCT_VSCODE
        if cli_identity:
            # Official CLI chat bodies drop Responses fields and pin retention.
            attempt_body = {k: v for k, v in attempt_body.items()
                            if k not in wb_identity.CLI_BODY_DROP_FIELDS}
            attempt_body["cache_retention"] = wb_identity.CLI_CACHE_RETENTION
        attempt_data = retry.encode_upstream_body(attempt_body)
        if cli_identity or vscode_identity:
            attempt_data = wb_identity.gzip_body(attempt_data)
        if session_key:
            conv_id = wb_identity.conversation_id_for(account.uid, session_key)
        else:
            conv_id = None
        try:
            req_headers = account.headers(purpose="chat", session_key=session_key,
                                          turn_request_id=turn_request_id,
                                          model=model, conversation_id=conv_id)
        except TypeError:
            # Duck-typed accounts (tests, probes) keep the bare signature.
            req_headers = account.headers(purpose="chat")
        if cli_identity or vscode_identity:
            req_headers["Content-Encoding"] = wb_identity.REQUEST_CONTENT_ENCODING
        req = urllib.request.Request(chat_url, data=attempt_data, method="POST",
                                     headers=req_headers)
        # Header-name fidelity: Request capitalizes names; the pool sends these
        # ordered pairs verbatim on the chat wire.
        req.wb_exact_headers = list(req_headers.items())
        try:
            resp = wb_accounts.urlopen(req, timeout=header_timeout,
                                       proxy=account.proxy)
            _apply_stream_idle_timeout(resp, idle_timeout)
            # A served request clears every account-level penalty
            # (soft streak, breaker, degrade) along with the model park.
            account.note_success(model=model)
            reset_switch_counter(account, model)
            # The slot stays held until the body is closed, not the headers.
            bind_account_slot_to_response(resp, account)
            slot_held = None
            return resp, account
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    detail = retry.read_http_error(exc, 600).decode("utf-8", "replace")
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
                # the short ladder, never the credential ladder.
                if reset_at is None and rate_limit_is_soft_shape(detail):
                    wait = account.note_unscoped_rate(model)
                    log("account %s: unscoped 429, parking '%s' for %.0fs (streak %d)"
                        % (account.uid[:8], model, wait,
                           getattr(account, "unscoped_streak", 0)))
                capped_at = time.time() if retry.rate_limit_is_cap(detail) else 0.0
                account.note_error("HTTP 429 (model throttled)", model=model,
                                   until=reset_at, cooldown=wait, cap_at=capped_at)
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
                    detail = retry.read_http_error(exc, 600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                if restriction_is_account_level(detail):
                    # Park it persistently and rotate; every other 403 stays a
                    # content-review passthrough. No 3s fallback for one account.
                    account.note_restricted(cooldown=RESTRICTED_403_COOLDOWN)
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
                    detail = retry.read_http_error(exc, 400).decode("utf-8", "replace")
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
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                if account.uid not in refreshed_uids:
                    refreshed_uids.add(account.uid)
                    try:
                        refreshed_ok = account.refresh()
                    except Exception as refresh_exc:
                        refreshed_ok = False
                        log("account %s: token refresh after 401 failed: %s"
                            % (account.uid[:8], exc_detail(refresh_exc, 160)))
                    if refreshed_ok:
                        log("account %s: HTTP 401, token refreshed; retrying the same account"
                            % account.uid[:8])
                        tried.discard(account.uid)
                        refresh_uid = account.uid
                        last_error = exc
                        continue
                log("account %s rejected (HTTP 401), rotating" % account.uid[:8])
                try:
                    detail = retry.read_http_error(exc, 200).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                account.note_error(retry.http_error_message(exc, detail, 160),
                                   cooldown=60,
                                   single_account=(total <= 1))
                last_error = exc
                continue
            if exc.code >= 500:
                detail = retry.http_error_detail(exc)
                code = retry.upstream_error_code(detail)
                if (code in retry.MODEL_SCOPED_ERROR_CODES
                        and not retry.model_scoped_errors_penalize_account()):
                    # A bad model name must not take the whole pool down: stop
                    # rotating and hand the upstream body back unchanged.
                    log("upstream %s for '%s' (code %s, model-scoped); "
                        "not charging the account" % (exc.code, model, code))
                    last_error = exc
                    break
                transient_hits += 1
                account.note_failure("HTTP %d" % exc.code)
                log("upstream %s for '%s' on %s (code %s), retrying (fails=%d)"
                    % (exc.code, model, account.uid[:8],
                       code if code is not None else "-", account.fails))
                if session_key and pool:
                    pool.affinity.unbind(session_key)
                last_error = exc
                continue
            raise
        except Exception as exc:
            if retry.is_tls_verify_error(exc):
                # The chain is the same for every account: one attempt is
                # enough; retrying only multiplies failed handshakes.
                last_error = exc
                break
            if session_key and pool:
                pool.affinity.unbind(session_key)
            if looks_like_local_network(exc):
                # Host offline: charge nobody, retry in place a few rounds.
                local_net_hits += 1
                if local_net_hits == 1:
                    log("local network looks down (%s); not charging any account"
                        % exc_detail(exc))
                last_error = exc
                time.sleep(min(0.3 * local_net_hits, 1.5))
                continue
            if is_transient(exc):
                transient_hits += 1
                log("upstream connection hiccup for '%s' on %s (%s), retrying"
                    % (model, account.uid[:8], exc_detail(exc)))
                last_error = exc
                time.sleep(min(0.6 * transient_hits, 2.0))
                continue
            detail = exc_detail(exc, 120)
            if not looks_like_local_network(exc):
                account.note_unknown_failure(detail)
            cooldown = 3.0 if total <= 1 else 60.0
            account.note_error(detail, cooldown=60, single_account=(total <= 1))
            # Say who was charged and for how long: the empty-pool error only
            # names accounts already cooling, never the one that charged them.
            log("account %s: %s, cooling %.0fs (fails=%d, ready=%d)"
                % (account.uid[:8], detail, cooldown, account.fails, total))
            last_error = exc
            continue
    if slot_held is not None:
        account_slot_release(slot_held)
        slot_held = None
    if last_error is not None:
        # Carry the failing account out to the caller, because the local
        # variable that would have held it was never assigned there.
        try:
            last_error.account_uid = last_uid
        except Exception:
            pass
        if retry.is_tls_verify_error(last_error):
            exc = retry.TLSVerifyError(retry.tls_verify_hint(last_error))
            exc.account_uid = last_uid
            raise exc
        if looks_like_local_network(last_error):
            # Report "host has no network", never "upstream unreachable".
            exc = LocalNetworkError(exc_detail(last_error))
            exc.account_uid = last_uid
            raise exc
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
        # Say which model and how long: a bare "frequency limit" invites a retry.
        reason = ("every usable account in realm '%s' is cooling the upstream "
                  "%s window; the pool resumes in %ds" % (realm, model, wait))
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait,
                          message=reason)
    if busy:
        # Every account is healthy, just serving its cap: say so instead of
        # blaming account state.
        reason = ("every usable account is already serving %d concurrent "
                  "request(s) (account_max_concurrent=%d); retry shortly or "
                  "raise the cap" % (slot_limit, slot_limit))
        raise RateLimited(None, reason, wait=1, message=reason)
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
    raise RuntimeError("no usable account for realm '%s': every candidate is "
                       "disabled, cooling down, expired, out of upstream quota "
                       "or parked by a local limit%s" % (realm, detail))
