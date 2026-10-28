"""Per-account penalties: cooldowns, failure streaks and model windows."""

import os
import time

from gateway.account.backoff import (BREAKER_THRESHOLD, DEGRADE_THRESHOLD,
                                     FAIL_WINDOW, breaker_backoff,
                                     degrade_backoff, next_local_4am,
                                     soft_backoff, unverified_soft_backoff)
from gateway.common.log import log


def auth_failure_text(text):
    """Credential-shaped 401, not an HTML proxy page; `invalid_token` wins."""
    lower = str(text or "").lower()
    if "invalid_token" in lower or "invalid-token" in lower:
        return True
    if "<html" in lower:
        return False
    return ("401" in lower or "unauthorized" in lower or "authorization" in lower
            or "token expired" in lower)


def restore_model_windows(raw):
    """Load unexpired 429 windows from {model: epoch} or a list of rows."""
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


def restore_cap_marks(raw):
    """Cap moments (model -> epoch) carried by the same window rows, or {}."""
    rows = raw if isinstance(raw, list) else []
    marks = {}
    now = time.time()
    for row in rows:
        if not isinstance(row, dict) or not row.get("cap"):
            continue
        try:
            moment = float(row.get("capAt") or 0.0)
            deadline = float(row.get("expiresAt") or 0.0)
        except (TypeError, ValueError):
            continue
        name = str(row.get("model") or "").strip()
        if name and moment and deadline > now:
            marks[name] = moment
    return marks


class HealthMixin(object):
    """Cooldown and failure state; all fields are owned by Account."""

    def note_balance_cooled(self, message="insufficient credits"):
        """Out of credits: park until the next local 04:00."""
        until = next_local_4am()
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self.balance_until = max(self.balance_until, until)
        return until

    def revive_balance_cooldown(self):
        """Lift the 402 park once a refresh shows credits again."""
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
        """Account-level 429: soft cooldown with exponential backoff."""
        with self._throttle_lock:
            self._decay_soft_streak()
            self.soft_streak += 1
            wait = soft_backoff(self.soft_streak)
            self.last_error = str(message)[:200]
            self.cooldown_until = max(self.cooldown_until, time.time() + wait)
        return wait

    def note_unscoped_rate(self, model):
        """Unscoped 429: grow its own streak and return this model's wait."""
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
        """Reset both rate ladders after FAIL_WINDOW; caller holds the lock."""
        now = time.time()
        if FAIL_WINDOW and self.last_soft_at and now - self.last_soft_at > FAIL_WINDOW:
            self.soft_streak = 0
            self.unscoped_streak = 0
        self.last_soft_at = now

    def note_failure(self, message):
        """5xx or transport failure: feed the breaker counter."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            self._decay_strikes()
            self.fails += 1
            if self.fails >= BREAKER_THRESHOLD:
                wait = breaker_backoff(self.fails)
                self.breaker_until = max(self.breaker_until, time.time() + wait)
        return self.breaker_until

    def note_unknown_failure(self, message):
        """Unknown error: feed both the degrade and breaker counters."""
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
        """Clear every account-level penalty after a served request."""
        changed = False
        with self._throttle_lock:
            if model:
                changed = self.model_cooldowns.pop(model, None) is not None
                self.model_cap_marks.pop(model, None)
            if self.restricted_until:
                # A served request is the only proof the vendor lifted it.
                self.restricted_until = 0.0
                changed = True
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
            self.token_expired = False
        if changed:
            self._persist_state()

    def note_restricted(self, cooldown=None):
        """Upstream account-level restriction (403/11140): park it, persisted."""
        if cooldown is None:
            from gateway.upstream.retry import RESTRICTED_403_COOLDOWN
            cooldown = RESTRICTED_403_COOLDOWN
        now = time.time()
        with self._throttle_lock:
            self.last_error = "HTTP 403 (account restricted)"
            # Only the first strike starts the clock: retries must not extend it.
            if self.restricted_until <= now:
                self.restricted_until = now + max(1.0, float(cooldown))
            until = self.restricted_until
        self._persist_state()
        return until

    def _set_last_error(self, message):
        """Record a refresh error in the account's runtime state."""
        with self._throttle_lock:
            self.last_error = str(message)[:200]

    def note_error(self, message, cooldown=60, single_account=False, model=None,
                   until=None, cap_at=0.0):
        """Park the account, or only `model` when the limit is model-scoped."""
        persist_window = False
        with self._throttle_lock:
            self.last_error = str(message)[:200]
            if auth_failure_text(message):
                self.token_expired = True
            if model:
                wait = max(1.0, float(until) - time.time()) if until else (
                    3.0 if single_account else float(cooldown))
                self.model_cooldowns[model] = time.time() + wait
                if cap_at:
                    # Upstream said quota exhausted (6004), not a short throttle.
                    self.model_cap_marks[model] = float(cap_at)
                persist_window = True
            else:
                actual_cooldown = 3 if single_account else cooldown
                self.cooldown_until = time.time() + actual_cooldown
        if persist_window:
            self._persist_state()

    def _persist_state(self):
        """Write the credential now; never raises into the caller."""
        if not self.path:
            return
        try:
            self.save(os.path.dirname(self.path))
        except Exception as exc:
            log("account %s: model window save failed: %s" % (self.uid, exc))

    def throttle_wait(self, model=None):
        """Seconds until this account can serve `model` again (0 = now)."""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        with self._throttle_lock:
            wait = max(0.0, self.cooldown_until - now,
                       self.balance_until - now, self.breaker_until - now,
                       self.degrade_until - now, self.restricted_until - now)
            if model:
                wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None):
        """Drop stored errors and cooldowns, for one model or all of them."""
        changed = False
        with self._throttle_lock:
            if model:
                changed = self.model_cooldowns.pop(model, None) is not None
                self.model_cap_marks.pop(model, None)
            else:
                changed = bool(self.model_cooldowns)
                self.model_cooldowns.clear()
                self.model_cap_marks.clear()
            if self.last_error or self.cooldown_until or self.balance_until:
                self.last_error = ""
                self.cooldown_until = 0
                self.balance_until = 0.0
            # restricted_until stays: it is the vendor's mark, not our cooldown.
        if changed:
            self._persist_state()
