"""Background credit refresher."""

import threading
import time

from gateway.common.log import log


class CreditsRefresher(threading.Thread):
    """Keep account credit balances fresh, off the request path.

    The dispatch preference reads `account.credits`, which only the sign-in and
    daily-activity tasks used to refresh: an account could go days without an
    update, and a restart inherited whatever balance was on disk. Every tick
    this refreshes the single stalest account that is past the TTL, so the
    upstream billing calls stay bounded - one per tick - and land spread across
    the pool instead of arriving as a burst. A failed account is parked for six
    hours so a broken credential cannot be retried on every tick.

    The request volume is capped by the tick, not by the pool size: at the
    default half-hour tick that is at most 48 billing calls a day however many
    accounts there are. With a 12-hour TTL a 28-account pool wants 56 calls a
    day, so the tick - not the TTL - is the binding limit and the effective
    refresh age settles a little above the TTL. That is the intended trade:
    the preference is measured in days, so a few extra hours of drift is worth
    far less than the calls it saves.
    """

    def __init__(self, pool, interval_seconds=1800):
        super().__init__(daemon=True, name="credits-refresher")
        self.pool = pool
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.last_run = None
        self.last_uid = None
        self.last_error = None
        self.logs = []
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._parked = {}          # uid -> epoch until which we skip it
        self._failed_cooldown = 6 * 3600.0

    def log(self, msg):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append("[%s] %s" % (stamp, msg))
        if len(self.logs) > 40:
            self.logs = self.logs[-40:]

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def wake(self):
        self._wake.set()

    def ttl_seconds(self):
        """The current TTL in seconds - the one place the unit is applied."""
        try:
            from gateway import settings as wb_settings
            hours = wb_settings.credits_refresh_hours(self.pool.dir)
        except Exception:
            return 0.0
        try:
            return max(0.0, float(hours)) * 3600.0
        except (TypeError, ValueError):
            return 0.0

    def stalest_account(self, ttl_seconds, now=None):
        """The account whose balance is oldest and past the TTL, or None.

        A balance that was never fetched counts as infinitely old, so a fresh
        install fills in its accounts before any timed-out one is revisited.
        """
        now = time.time() if now is None else now
        with self.pool._lock:
            accounts = list(self.pool.accounts)
        chosen = None
        chosen_age = None
        for account in accounts:
            if not account.enabled or not account.access_token:
                continue
            if self._parked.get(account.uid, 0) > now:
                continue
            credits = account.credits
            updated = credits.get("updated_at") if isinstance(credits, dict) else None
            if isinstance(updated, (int, float)):
                age = now - updated
            else:
                age = float("inf")
            if age < ttl_seconds:
                continue
            if chosen_age is None or age > chosen_age:
                chosen, chosen_age = account, age
        return chosen

    def refresh_once(self, now=None):
        """Refresh one stale account; returns its uid, or None when none was due."""
        ttl = self.ttl_seconds()
        if ttl <= 0:
            return None
        with self._lock:
            account = self.stalest_account(ttl, now=now)
            if account is None:
                return None
            try:
                result = account.fetch_credits()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            if not isinstance(result, dict) or not result.get("ok"):
                error = (result or {}).get("error") or "unknown error"
                self._parked[account.uid] = \
                    (time.time() if now is None else now) + self._failed_cooldown
                self.last_error = str(error)
                self.log("刷新 %s 积分失败，暂缓一小时: %s"
                         % (account.uid[:8], error))
                return None
            self.last_run = time.time() if now is None else now
            self.last_uid = account.uid
            self.last_error = None
            self.log("已刷新 %s 的积分（%s）"
                     % (account.uid[:8], (account.credits or {}).get("updated_iso")))
            return account.uid

    def run(self):
        while not self._stop_event.is_set():
            self._wake.wait(timeout=self.interval_seconds)
            if self._stop_event.is_set():
                break
            self._wake.clear()
            try:
                self.refresh_once()
            except Exception as exc:
                self.last_error = str(exc)
                self.log("刷新循环异常: %s" % exc)

    def status(self):
        return {
            "ttl_hours": round(self.ttl_seconds() / 3600.0, 2),
            "interval_seconds": self.interval_seconds,
            "last_run": self.last_run,
            "last_uid": self.last_uid,
            "last_error": self.last_error,
            "logs": self.logs[-10:],
        }
