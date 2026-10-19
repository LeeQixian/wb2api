# protocol

Translates between the OpenAI API shapes and the WorkBuddy upstream protocol.

## Design

The package is split by concern:

- `state.py` - mutable module-level state (active realm, system prompt, model cache).
- `model_info.py` - model id classification, realm routing, catalog metadata.
- `sanitize.py` - fingerprint rewriting (Codex / Claude Code WAF patterns).
- `repair.py` - tool-call pairing repair, role normalization, reasoning backfill.
- `stream.py` - SSE frame cleanup and chat-stream aggregation.
- `tools.py` - DSML fallback parsing, custom-tool downgrade, namespace expand/restore.
- `request.py` - outbound chat body construction and guard switches.
- `responses_in.py` - Responses API input translated down to chat messages.
- `responses_out.py` - chat completions translated back up into Responses objects.
- `usage.py` - usage normalization across client spellings.

`gateway/translate.py` at the top level is the import surface for this
package: it exposes every symbol as `gateway.translate.X`, and reads and
writes of the mutable state names go through to `protocol/state.py`, so
`gateway.translate.CURRENT_REALM = "cn"` changes the running realm.

## Invariants

- **`tool_choice="none"` keeps the tools declaration.** The upstream does not
  accept an object form (it returns 11101), so only the field is set to the
  string `"none"`; the declarations stay. Removing them makes the model emit
  an unparseable call and the client loop.
- **An empty `function_call` placeholder is never a real call.** The upstream
  ends some streams with `function_call:{"name":"","arguments":""}`; a call
  with no name and no arguments is dropped, or it would fabricate a tool_call
  and flip `finish_reason`.
- **`developer` is folded to `system`.** The upstream rejects the newer OpenAI
  role with code 11128.
- **Assistant messages carry non-empty `reasoning_content` in thinking mode.**
  A missing or empty value is rejected (11155); a value is mirrored in.
- **Namespace tools are expanded outbound and restored inbound.** The upstream
  knows only flat functions; the client needs the namespace field back or it
  answers "unsupported call".
- **Custom (freeform) tools become a single-string function** outbound and are
  re-inflated to `custom_tool_call` inbound.
- **Tool schemas are normalized recursively.** Type aliases (`str`/`int`/
  `dict`), null metadata, tuple-form `items`, `required` and `$defs`/`anyOf`
  bodies are all rewritten in place; a nested definition is validated too.
- **A `tool_result` batch is packed adjacent to its `tool_calls`.** The
  upstream rejects an intruding message between them (11148), so a non-result
  message before or inside the batch is moved behind the whole batch.
- **`WB_PRESERVE_REQUEST_CONTRACT=1` skips fingerprint sanitizing**
  (tool arguments and tool results verbatim, `strict` kept on namespace
  sub-tools) and maps Responses `text.format` onto `response_format`.

## Config

- `prompt.mode` / `prompt.file` - system-prompt mode (passthrough/custom/append).
- `local_web_tools` - enables gateway-run web_search/web_fetch.
- `daily_token_limit`, `reserve_credits`, `expiring_window_days` - dispatch guards.
