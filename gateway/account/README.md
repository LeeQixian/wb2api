# account

WorkBuddy accounts: credentials, outbound requests, state and the pool.

## Design

- `model.py` - `Account`: one credential with its runtime state (headers,
  cooldowns, credits, token refresh).
- `maintenance.py` - `MaintenanceMixin`: check-in, daily activity and the web
  session, mixed into `Account`.
- `pool.py` - `AccountPool`: load, pick an account for a request, dispatch
  guards, enable/disable, account resolution, the login flow.
- `jwt.py` - JWT claims, realm detection, per-realm endpoint config.
- `transport.py` - urlopen plus JSON with retries and per-account proxy support.
- `backoff.py` - soft-rate / breaker / degrade windows, park-until-04:00.
- `credentials.py` - credential-shape parsing, profile fetch, credit expiry.
- `credits.py` - `CreditsRefresher`, the background balance refresh thread.
- `affinity.py` - session-to-account binding.
- `web_config.py` - web-console endpoints and constants.

`gateway/accounts.py` at the top level is the import surface for this package
(`gateway.accounts.X`); writes to and patches of the transport helpers go
through to `account/transport.py`.

## Invariants

- **The realm comes from the token issuer first, then the domain.** The domain
  is a client-side field that may be stale or belong to the other realm; a
  stale domain is realigned to the token's realm so the `X-Domain` header
  cannot drag a request to the wrong exit.
- **Enterprise accounts have no personal growth system.** Check-in, growth
  tasks and travel are skipped for them instead of taking a 403.
- **`lastError` and `cooldownUntil` are runtime-only.** They are never
  persisted, or a recovered rate-limit label would return on restart.
- **An out-of-credits account parks until the next local 04:00** (the
  overnight reset); a balance above zero releases it early.
- **Backoff ladder.** Account-level 429 doubles 10min -> 2h; three consecutive
  hard failures trip a 30min -> 6h breaker; five unknown failures degrade
  10min -> 2h. One success clears everything.
- **A 401 does not by itself mean the credential is dead.** Only a token-shaped
  failure marks `token_expired` (a middle-layer HTML 401 page does not); the
  flag gates dispatch and forces a lazy refresh, instead of parking the account.
- **`AccountPool.resolve()`** accepts a full uid, a unique uid prefix, or a
  unique nickname; an ambiguous reference is an error, never a silent pick.

## Config

From `accounts/settings.json`:

- `reserve_credits` - stop dispatching an account at or below this balance.
- `expiring_window_days` - prefer accounts whose credits expire within N days.
- `credits_refresh_hours` - staleness threshold for the background refresher.
- `daily_chat_web` - whether intl daily activity uses the web channel.
- `auto_switch_product` - rotate the outbound identity on a 429.
