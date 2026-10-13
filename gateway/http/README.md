# http

HTTP layer: the request handler, its state, and the routes.

## Design

`gateway/server.py` is the entry point (`python3 -m gateway.server`). It is a
thin facade over this package and forwards state writes (`POOL`, `ARGS`,
`ACCOUNTS_DIR`, `USAGE_DIR`, ...) onto `http/state.py`.

- `state.py` - shared runtime state plus `refresh_pool()`.
- `handler.py` - `Handler`, assembled from the mixins below.
- `base.py` - request parsing, auth, CORS, error envelopes.
- `routes/get.py` - `GET /health`, `GET /realm`, `GET /v1/models`, `GET /credits`.
- `routes/post.py` - `POST /realm`, `POST /v1/chat/completions`, `POST /v1/responses`.
- `runner.py` - `GatewayServer` (backlog) and `build_server`.
- `usage.py` - `usage.jsonl` row writing.
- `errors.py` - `BodyTooLarge`, `BadJSON`.

`gateway/main.py` parses arguments, wires the pool/settings and starts the
server; `gateway/common/log.py` is the shared log sink.

## What each piece fixes

- **Auth surface.** The key is read only from the `Authorization` / `x-api-key`
  / `api-key` headers. The `?key=` spelling is gone because it leaks the key
  into browser history and Referer. Binds `127.0.0.1` by default; CORS stays
  off unless `--cors`.
- **Nagle / backlog.** `disable_nagle_algorithm` plus `request_queue_size = 128`
  stop small JSON replies from paying a delayed-ACK penalty and stop SYN drops
  under bursts.
- **Body cap.** A 16MB payload cap (`WB_MAX_PAYLOAD_BYTES`) bounds memory; the
  body is read whole before parsing.
- **Chat slots.** A bounded semaphore caps concurrent chats
  (`WB_MAX_CONCURRENT_CHAT`), with `WB_CHAT_SLOT_WAIT` for the wait.

## Unresolved

- Route registration is a hand-written dispatch in `routes/*`; there is no
  route table, so adding a route means editing both `do_GET`/`do_POST`.

## Config fields

- `--host`, `--port`, `--api-key`, `--realm`, `--tasks`, `--cors`,
  `--accounts-dir`, `--usage-dir`.
- Env: `WB_MAX_PAYLOAD_BYTES`, `WB_MAX_CONCURRENT_CHAT`, `WB_CHAT_SLOT_WAIT`.
