# protocol

OpenAI <-> WorkBuddy protocol translation.

## Design

`gateway/translate.py` is a facade: it re-exports every symbol from this
package as `gateway.translate.X` and proxies reads/writes of the mutable
state names (`CURRENT_REALM`, `SYSTEM_PROMPT`, `PROMPT_FILE_OVERRIDE`,
`ACCOUNTS_DIR`, `_models_cache`) onto `protocol/state.py`, so callers and
tests that mutate `translate.CURRENT_REALM` keep working.

The implementation is split by concern:

- `state.py` - mutable module-level state (realm, system prompt, model cache).
- `model_info.py` - model id classification, realm routing, catalog metadata.
- `sanitize.py` - fingerprint rewriting (Codex / Claude Code WAF patterns).
- `repair.py` - tool-call pairing repair, role normalization, reasoning backfill.
- `stream.py` - SSE frame cleanup and chat-stream aggregation.
- `tools.py` - DSML fallback parsing, custom-tool downgrade, namespace expand/restore.
- `request.py` - outbound chat body construction and guard switches.
- `responses_in.py` - Responses API input translated down to chat messages.
- `responses_out.py` - chat completions translated back up into Responses objects.
- `usage.py` - usage normalization across client spellings.

## What each piece fixes

- **Empty function_call placeholders.** The upstream ends streams with
  `function_call:{"name":"","arguments":""}`. A truthiness test treats the
  non-empty dict as real, so the placeholder survives, fabricates a tool_call
  with a fake id, and flips `finish_reason` to `tool_calls`; strict clients
  then wait forever for arguments that never come. `stream.py` checks name and
  arguments explicitly instead.
- **`tool_choice="none"`.** Deleting the `tools` declaration on "none" made
  the model downgrade its call into unparseable text and loop, so the
  declaration is kept and only the field itself is set to `"none"`. The
  upstream takes a string, so an object form must degrade or it returns 11101.
- **Orphan tool-call pairing.** A call whose result never returned, or results
  split from their batch by an interleaved message, makes the upstream reject
  every later turn of that conversation. `repair.py` pairs and drops them
  before the body leaves.
- **role 11128.** OpenAI's newer `developer` role is folded to `system`;
  sending it verbatim fails with code 11128.
- **reasoning backfill (11155).** While thinking is on, every assistant
  message needs a non-empty `reasoning_content`; empty placeholders are not
  enough, so a value is mirrored in.
- **Namespace tools.** Codex sends MCP/plugin tools as `type="namespace"`. The
  upstream only knows flat functions, so they are expanded on the way out and
  restored (namespace field re-attached) on the way back; without the restore
  the app answers "unsupported call".
- **Custom (freeform) tools.** Downgraded to a single-string `input` function
  outbound and re-inflated to `custom_tool_call` inbound.

## Unresolved

- `responses_out.py:stream_responses_events` (~490 lines) and
  `responses_in.py:_responses_input_to_messages` (~210 lines) are large single
  functions; they were moved verbatim, not restructured. Any split needs its
  own test pass.

## Config fields

Read through `gateway.config.settings`:

- `prompt.mode` / `prompt.file` - system-prompt mode (passthrough/custom/append).
- `local_web_tools` - enables gateway-run web_search/web_fetch.
- `daily_token_limit`, `reserve_credits`, `expiring_window_days` - dispatch guards.
