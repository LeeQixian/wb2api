# upstream

Outbound requests to the WorkBuddy upstream.

## Design

`gateway/upstream/__init__.py` is the facade (`gateway.upstream.X`). It also
forwards writes of `_POOL` / `USAGE_DIR` / `ACCOUNTS_DIR` / `_lock` onto
`upstream/state.py`, and forwards `wb_accounts` / catalog-function patches onto
the modules that use them, so tests patching the facade still take effect.

- `state.py` - shared mutable state injected by `server.main()`.
- `client.py` - `open_upstream()`: the retry / rotation loop.
- `pool.py` - keep-alive connection pool (see its module docstring).
- `catalog.py` - live `/v3/config` catalogue fetch, merge and curation.
- `realm.py` - global exit state (read, switch, persist).
- `retry.py` - 429/403/5xx classification and identity rotation.
- `guards.py` - daily token guard and usage-log folding.
- `timeouts.py` - socket header / idle timeouts.
- `errors.py` - `ContentRejected`, `RateLimited`.

## What each piece fixes

- **429 classification.** A cn-region 429 body states its reset in Chinese, so
  the reset time is parsed from both spellings; without it every cn 429
  degraded to an account-level cooldown. Account-level scope needs positive
  wording (a word boundary avoids matching identifiers like `accountId`), and
  an unscoped soft limit takes a separate short ladder (60s -> 600s).
- **403 risk control (code 11140).** "Account restricted due to unusual
  activity" is an account problem, not content review: the account cools 2h and
  the next one takes over. Content-review 403 still passes straight through,
  which is this project's contract.
- **>=500 rotates.** Any 5xx (including custom codes like 550) rotates instead
  of failing the request.
- **Connection reuse.** urllib's `do_open` hardcodes `Connection: close`, so
  every request pays a fresh TCP+TLS handshake. `pool.py` reuses `http.client`
  connections; toggle with `WB_UPSTREAM_KEEPALIVE=0`.
- **Timeouts.** A header timeout (first byte) plus a stream idle timeout stop a
  stuck stream from holding a concurrency slot for 10 minutes.

## Unresolved

- The identity auto-switch rotates `workbuddy -> vscode -> cli -> ...`; there
  is no per-identity health tracking beyond the round counter.

## Config fields

- `upstream.header_timeout_seconds`, `upstream.idle_timeout_seconds`.
- `daily_token_limit`, `auto_switch_product` (see `account/README.md`).
- Env: `WB_UPSTREAM_KEEPALIVE`, `WB_UPSTREAM_POOL_SIZE`, `WB_UPSTREAM_POOL_IDLE`.
