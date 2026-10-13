# config

Configuration and identity.

## Design

`gateway/settings.py`, `gateway/prompt.py`, `gateway/identity.py` and
`gateway/fingerprint.py` are small import facades over this package (kept so
existing imports and tests keep working).

- `settings.py` - reads/writes `accounts/settings.json`: the API key and the
  guard switches. Every switch has a conservative default.
- `prompt.py` - the gateway system-prompt modes (see `protocol/README.md`).
- `identity.py` - the three outbound identities (desktop / vscode / cli) with
  their endpoints, headers and user-agents.
- `fingerprint.py` - deterministic id derivation and desktop fingerprints.

## What each piece fixes

- **Three outbound identities.** The upstream rate-limits per identity, so a
  denied identity can rotate to another; the chosen identity is persisted per
  account and survives a restart.
- **Switches default off.** An untouched settings file keeps the gateway at its
  documented behaviour.

## Config fields

`accounts/settings.json`:

| key | default | effect |
|---|---|---|
| `api_key` | none | required on inference routes when set |
| `reserve_credits` | 0 | stop dispatching an account at/below this balance |
| `daily_token_limit` | 0 | park an account after this many tokens per day |
| `auto_switch_product` | off | rotate identity on a 429 |
| `daily_chat_web` | on | intl daily activity via the web channel |
| `local_web_tools` | off | gateway-run web_search / web_fetch |
| `expiring_window_days` | 7 | prefer accounts expiring within N days |
| `credits_refresh_hours` | 12 | background credit refresh staleness |
| `upstream.header_timeout_seconds` | 120 | first-byte timeout |
| `upstream.idle_timeout_seconds` | 300 | stream idle timeout |
| `prompt.mode` / `prompt.file` | passthrough | system-prompt mode |
