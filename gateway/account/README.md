# account

WorkBuddy accounts: credentials, OAuth, runtime state and the pool.

## Design

`gateway/accounts.py` is a facade re-exporting this package as
`gateway.accounts.X`; it also forwards patches of `urlopen`/`http_json` onto
`account/transport.py` so tests that monkeypatch the facade still intercept the
real calls.

- `model.py` - `Account`: credentials, outbound headers, cooldowns, credits.
  Business-heavy on purpose (OAuth, signing, credit parsing live here), so it
  is the one file allowed past 800 lines.
- `maintenance.py` - `MaintenanceMixin`: check-in, daily activity and the web
  session, mixed into `Account`. Split out so the non-maintenance surface stays
  readable.
- `pool.py` - `AccountPool`: load, pick, dispatch guards, enable/disable,
  nickname/prefix resolution, login flow.
- `jwt.py` - JWT claims, realm evidence, per-realm endpoint config.
- `transport.py` - urlopen + JSON with retries and proxy support.
- `backoff.py` - soft-rate / breaker / degrade windows, park-until-4am.
- `credentials.py` - credential-shape parsing, profile fetch, credit expiry.
- `credits.py` - `CreditsRefresher` background thread.
- `affinity.py` - session-to-account binding.
- `web_config.py` - web-console endpoints and constants.

## What each piece fixes

- **Realm evidence, never a blind guess.** The realm comes from the token
  issuer first, then the domain; the domain is a client-side field that may be
  stale or belong to the other realm. A stale domain is realigned to the
  token's realm so `X-Domain` cannot drag a request back to the wrong exit.
- **Enterprise accounts.** Enterprise space accounts have no personal growth
  or check-in system and report zero credits; the pool skips check-in, growth
  tasks and travel for them rather than taking a 403.
- **Cooldown persistence.** `lastError`/`cooldownUntil` are runtime-only and
  never persisted, or a recovered rate-limit label would come back on restart.
- **402 parking.** An out-of-credits account parks until the next local 04:00
  (the overnight reset); a credit refresh above zero releases it early.
- **Backoff ladder.** Account-level 429 doubles 10min -> 2h; three consecutive
  hard failures trip a 30min -> 6h breaker; five unknown failures degrade
  10min -> 2h. One success clears everything.
- **Nickname resolution.** `AccountPool.resolve()` accepts a full uid, a unique
  uid prefix, or a unique nickname, so `wbctl enable/disable` does not need the
  full uuid. Ambiguity is an error, never a silent pick.

## Unresolved

- `model.py` is 826 lines. Kept whole because the Account surface is genuinely
  broad; split only if a clear seam appears.

## Config fields

`accounts/settings.json` (through `gateway.config.settings`):
- `reserve_credits` - stop dispatching an account at or below this balance.
- `expiring_window_days` - prefer accounts whose credits expire within N days.
- `credits_refresh_hours` - staleness threshold for the background refresher.
- `daily_chat_web` - whether intl daily activity uses the web channel.
- `auto_switch_product` - rotate the outbound identity on a 429.
