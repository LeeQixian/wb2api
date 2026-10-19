# http

The HTTP layer: the request handler, its routes, and the runtime state.

## Design

- `state.py` - runtime state (the active pool, args, dirs, chat slots) and
  `refresh_pool()`.
- `handler.py` - `Handler`, assembled from the mixins below.
- `base.py` - request parsing, auth, CORS, error envelopes.
- `routes/get.py` - `GET /health`, `GET /realm`, `GET /v1/models`, `GET /credits`.
- `routes/post.py` - `POST /realm`, `POST /v1/chat/completions`, `POST /v1/responses`.
- `runner.py` - `GatewayServer` and `build_server`.
- `usage.py` - `usage.jsonl` row writing.
- `errors.py` - `BodyTooLarge`, `BadJSON`.

`gateway/server.py` is the entry point (`python3 -m gateway.server`); it starts
the server and exposes the module-level state. `gateway/main.py` parses
arguments and wires everything up. `gateway/common/log.py` is the shared sink.

## Invariants

- **The API key is read only from headers** (`Authorization` / `x-api-key` /
  `api-key`). There is no `?key=` query form, so a key can never leak into
  browser history or a Referer. Binds `127.0.0.1` by default; CORS is off
  unless `--cors` is passed.
- **A response is flushed with `disable_nagle_algorithm`** and the listen
  backlog is raised, so small JSON replies do not pay a delayed-ACK penalty
  and bursts are not dropped.
- **The request body is capped** (`WB_MAX_PAYLOAD_BYTES`, default 16MB) and
  read whole before parsing.
- **Concurrent chats are bounded** by a semaphore (`WB_MAX_CONCURRENT_CHAT`).

## Config

- `--host`, `--port`, `--api-key`, `--realm`, `--tasks`, `--cors`,
  `--accounts-dir`, `--usage-dir`.
- Env: `WB_MAX_PAYLOAD_BYTES`, `WB_MAX_CONCURRENT_CHAT`, `WB_CHAT_SLOT_WAIT`.
