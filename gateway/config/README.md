# config

Configuration, outbound identity and request fingerprints.

## Design

- `settings.py` - reads and writes `accounts/settings.json`: the API key and
  the guard switches. Every switch has a conservative default, so an untouched
  file keeps the gateway at its documented behaviour.
- `prompt.py` - the gateway system-prompt modes (passthrough / custom /
  append); see `protocol/README.md` for how they are applied.
- `identity.py` - the three outbound identities (desktop / vscode / cli), each
  with its endpoints, headers and user-agents.
- `fingerprint.py` - deterministic id derivation and desktop fingerprints.

`gateway/settings.py`, `gateway/prompt.py`, `gateway/identity.py` and
`gateway/fingerprint.py` at the top level are the import surface for this
package.

## Invariants

- **The upstream rate-limits per identity.** The three identities are three
  separate quota lines, so a denied one can rotate to another. The identity
  chosen for an account is persisted and survives a restart.
- **Switches default to their safe value.** Nothing is enabled by writing an
  empty settings file.

## Config

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
