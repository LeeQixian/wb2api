# upstream

Outbound requests to the WorkBuddy upstream: the retry/rotation loop, the
connection pool, the live model catalogue and the dispatch guards.

## Design

- `client.py` - `open_upstream()`: pick an account, send the request, and run
  the retry/rotation loop on failure.
- `pool.py` - a keep-alive connection pool keyed by (scheme, host, port,
  proxy, credential digest): two accounts never share one TCP/TLS connection.
  Chat requests carry `wb_exact_headers` so header names go out verbatim.
- `catalog.py` - fetch the live `/v3/config` catalogue, merge it over the
  bundled snapshot, and curate the advertised model list.
- `realm.py` - the active exit (intl/cn): read, switch and persist.
- `retry.py` - classify a failure (429 / 403 / 5xx) and decide the response.
- `guards.py` - the daily token guard and the usage-log folding.
- `timeouts.py` - socket header and stream-idle timeouts.
- `errors.py` - `ContentRejected`, `RateLimited`.
- `state.py` - shared runtime state (the active pool, usage dir, coarse lock).

`gateway/upstream/` is its own import surface (`gateway.upstream.X`); writes to
the shared state go through to `upstream/state.py`.

## Invariants

- **A 429 names its reset in both English and Chinese**, so both spellings are
  parsed; an unscoped soft limit takes a separate short ladder (60s -> 600s),
  not the account-level one.
- **403 code 11140 is account risk control, not content review.** The account
  cools for 2h and the next one takes over. A content-review 403 passes
  straight through to the client.
- **Any status >= 500 rotates** to another account (custom codes included).
- **Retries only cover failures before any response byte arrives** on a reused
  connection; a request the peer may have processed is never retried.
- **A stuck stream does not hold a slot forever**: a first-byte timeout and a
  stream-idle timeout both apply.
- **A 401 refreshes the account's token once before rotating**; only a failed
  refresh or a second 401 moves on. A certificate-verification failure is a
  config error: no retry, and the answer names WB_CA_BUNDLE.
- **Chat bodies are compact JSON** (`encode_upstream_body`), matching Node's
  `JSON.stringify`; other paths keep urllib defaults.

## Config

- `upstream.header_timeout_seconds`, `upstream.idle_timeout_seconds`.
- `daily_token_limit`, `auto_switch_product` (see `account/README.md`).
- Env: `WB_UPSTREAM_KEEPALIVE`, `WB_UPSTREAM_POOL_SIZE`, `WB_UPSTREAM_POOL_IDLE`,
  `WB_CA_BUNDLE` / `SSL_CERT_FILE` (private root CA bundle).
