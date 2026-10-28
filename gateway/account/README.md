# account

WorkBuddy accounts: credentials, outbound requests, state and the pool.

## Design

- `model.py` - `Account`: credential, readiness, serialization, headers, refresh
  and credit fetch.
- `health.py` - `HealthMixin`: cooldown ladders, failure streaks and persisted
  model windows.
- `packages.py` - billing package parsing and the expiring-credits preference.
- `maintenance.py` - `MaintenanceMixin`: check-in, daily activity, web sessions
  and the bounded session ledger.
- `pool.py` - `AccountPool`: load, pick, dispatch guards, enable/disable,
  account resolution, login flow.
- `jwt.py` - JWT claims, realm detection, per-realm endpoint config.
- `transport.py` - urlopen plus JSON with retries and per-account proxy support.
- `backoff.py` - soft-rate, breaker and degrade windows, park-until-04:00.
- `credentials.py` - credential parsing, profile fetch, credit expiry text.
- `credits.py` - `CreditsRefresher`, the background balance refresh thread.
- `affinity.py` - session-to-account binding.
- `web_config.py` - web-console endpoints and constants.

`gateway/accounts.py` is the import surface (`gateway.accounts.X`); transport
helpers resolve to `account/transport.py`.

## Invariants

- Realm comes from the token issuer, then the domain; a stale domain is
  realigned so `X-Domain` cannot point at the wrong exit.
- Enterprise accounts have no personal growth system; check-in, growth tasks and
  travel are skipped instead of taking a 403.
- `lastError` and `cooldownUntil` are runtime-only and never persisted.
- An out-of-credits account parks until the next local 04:00; a positive balance
  releases it early.
- Backoff: account 429 doubles 10min to 2h; three hard failures trip a 30min to
  6h breaker; five unknown failures degrade 10min to 2h. One success clears all.
- Only a token-shaped 401 sets `token_expired`; an HTML proxy 401 does not,
  unless its `WWW-Authenticate` names `invalid_token`. A served request clears
  the flag.
- A 403 / `code 11140` restriction persists in `restrictedUntil` and outlives
  restarts, refreshes and identity switches; only a served request lifts it.
- Model 429 windows persist as absolute epochs in the credential
  (`modelWindows`, rows carry `cap` / `capAt` for a 6004 quota cap) and lapse
  against the clock.
- `AccountPool.resolve()` accepts a full uid, a unique uid prefix or a unique
  nickname; ambiguity is an error.

## Config

From `accounts/settings.json`:

- `reserve_credits` - stop dispatching at or below this balance.
- `expiring_window_days` - prefer accounts whose credits expire within N days.
- `credits_refresh_hours` - staleness threshold for the background refresher.
- `daily_chat_web` - whether intl daily activity uses the web channel.
- `auto_switch_product` - rotate the outbound identity on a 429.
