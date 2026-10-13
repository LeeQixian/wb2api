"""Outbound chat request construction and guard switches."""


import hashlib
import json
import os
import re
from gateway import prompt as wb_prompt
from gateway import settings as wb_settings
from gateway import webtools as wb_webtools
from gateway.common.log import log
from gateway.protocol import state
from gateway.protocol.model_info import (ALLOWED_MODELS, model_default_effort, model_default_max_output_tokens)
from gateway.protocol.repair import (backfill_reasoning_content, cleanup_orphan_tool_calls, merge_adjacent_tool_calls, repack_tool_result_blocks)
from gateway.protocol.repair import normalize_roles
from gateway.protocol.sanitize import sanitize_messages


AFFINITY_BY_PREFIX = os.environ.get("WB_AFFINITY_BY_PREFIX", "1").lower() not in (
    "0", "false", "no", "off")
AFFINITY_DEBUG = os.environ.get("WB_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")
BLOCK_BACKGROUND_REQUESTS = False
BACKGROUND_TRIGGER_KEYWORDS = (
    "memory_consolidation",
    "memory-write",
    "memory_write",
    "memorywriting",
    "ambient",
    "suggestion",
    "auto_review",
    "auto-review",
    "autoreview",
    "title",
    "compaction",
    "compact",
    "summariz",
)
BACKGROUND_THREAD_SOURCES = (
    "memory_consolidation",
    "memory",
    "ambient",
    "suggestion",
    "auto_review",
    "autoreview",
    "title",
)
_CITATION_MD_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]+)\)")
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def set_system_prompt(text):
    state.set_system_prompt(str(text or state.DEFAULT_SYSTEM_PROMPT))


def build_upstream_body(payload):
    model = payload.get("model") or ""
    # Resolve the effective thinking state before the backfill below: while
    # thinking is on, the upstream requires reasoning_content on every
    # assistant message, whether or not the client kept a reasoning trace.
    thinking = payload.get("thinking")
    thinking_type = ""
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").strip().lower()
    effort = payload.get("reasoning_effort") or payload.get("reasoningEffort")
    thinking_enabled = False
    if str(model).lower().startswith("deepseek"):
        if thinking_type == "enabled":
            thinking_enabled = True
        elif thinking_type != "disabled" and str(effort or "").strip().lower() != "none":
            thinking_enabled = True
    messages = normalize_roles(payload.get("messages") or [])
    messages = sanitize_messages(messages)
    messages = backfill_reasoning_content(
        messages, model, thinking_enabled=thinking_enabled
    )
    # #160: gateway system-prompt mode. passthrough (default) keeps the
    # client's own system/developer messages and only tops up a neutral
    # placeholder when the request carries none; custom replaces the whole
    # block with the gateway prompt; append inserts after the leading run.
    mode, text = prompt_mode_config()
    messages = wb_prompt.apply_mode(messages, mode, text, placeholder=state.SYSTEM_PROMPT)
    body = dict(payload)
    # Private request markers ride along on the chat body for the Responses
    # path (the namespace map, the local-web-tools flag). They are not part of
    # the upstream protocol, so drop them here rather than trusting the
    # upstream to ignore unknown keys.
    for _marker in [k for k in body if str(k).startswith("_")]:
        body.pop(_marker, None)
    # dict(payload) copies the raw model name; overwrite it with the alias.
    body["model"] = model
    body["messages"] = messages
    # Repair tool-call pairing before the body leaves: a call whose result never
    # came back, or results split from their batch by an interleaved message,
    # makes the upstream reject every later turn of that conversation.
    # Order matters (#153): merge the split parallel-batch declarations
    # first, so repack sees one complete batch.
    repaired, _merged = merge_adjacent_tool_calls(body["messages"])
    repaired, _repacked = repack_tool_result_blocks(repaired)
    repaired, _cleaned = cleanup_orphan_tool_calls(repaired)
    body["messages"] = repaired
    translate_max_completion_tokens(body)
    normalize_tool_choice(body)
    normalize_tools(body)
    normalize_stop(body)
    # #121: Codex/Responses clients omit max_output_tokens by default; the
    # upstream then applies its own internal cap and long reasoning runs
    # exhaust before any text comes out. Default to the catalog's declared
    # maxOutputTokens when the client left it out. An explicit max_tokens /
    # max_completion_tokens always keeps precedence.
    if "max_tokens" not in body:
        default_max = model_default_max_output_tokens(model)
        if default_max:
            body["max_tokens"] = default_max
    # Thinking injection for DeepSeek models.
    #
    # thinking.type=enabled on its own does not switch the reasoning trace on:
    # the upstream still answers without one unless an effort level rides along.
    # Measured against the live upstream on deepseek-v4.1-flash, same prompt:
    #   enabled + no effort   -> reasoning_tokens 0,  reasoning_content len 0
    #   reasoning_effort=high -> reasoning_tokens 37, reasoning_content len 117
    # The client's own choice always wins; an effort level is only filled in
    # when it left the field out, and never for a request that opted out.
    if str(model).lower().startswith("deepseek"):
        thinking = body.get("thinking")
        opted_out = isinstance(thinking, dict) and \
            str(thinking.get("type") or "").strip().lower() == "disabled"
        effort = body.get("reasoning_effort") or body.get("reasoningEffort")
        if not opted_out and str(effort or "").strip().lower() != "none":
            if "thinking" not in body:
                body["thinking"] = {"type": "enabled"}
            if not effort:
                body["reasoning_effort"] = model_default_effort(model) or "high"
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def normalize_tool_choice(obj):
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        val = tc.strip().lower()
        if val == "none":
            # Keep the tools declaration when tool_choice is "none": without
            # the schema the model downgrades calls to unparseable text and the
            # agent loops. The field itself already expresses the intent.
            obj["tool_choice"] = "none"
        return
    if isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            # Same intent: keep tools. The upstream takes only a string, so an
            # object form must degrade to "none" or it returns 11101.
            obj["tool_choice"] = "none"
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            obj["tool_choice"] = name.strip() or "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)


def normalize_tools(obj):
    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        return
    norm = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Wrap top-level name tool definition into Chat Completions function schema
        if "name" in t and "function" not in t and t.get("type") == "function":
            fn = {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {},
            }
            if "strict" in t:
                fn["strict"] = t["strict"]
            norm.append({"type": "function", "function": fn})
        else:
            norm.append(t)
    obj["tools"] = norm


def translate_max_completion_tokens(obj):
    alias = obj.pop("max_completion_tokens", None)
    if alias is None:
        return
    if "max_tokens" in obj:
        return
    try:
        val = int(alias)
        if val > 0:
            obj["max_tokens"] = val
    except (TypeError, ValueError):
        pass


def normalize_stop(obj):
    """Upstream Go struct pins stop to []string; OpenAI allows a bare string.

    Empty/invalid values drop the field (hub c3f263d, code 11101).
    """
    if "stop" not in obj:
        return
    val = obj["stop"]
    if isinstance(val, str):
        val = [val] if val else []
    elif isinstance(val, (list, tuple)):
        val = [s for s in val if isinstance(s, str) and s]
    else:
        val = []
    if val:
        obj["stop"] = val
    else:
        obj.pop("stop", None)


def prompt_mode_config():
    """(mode, text) for this request; default ("passthrough", "").

    Read per request, so `wbctl set-prompt` takes effect on the next one
    without a restart (#160). A file that cannot be read fails open to the
    built-in prompt.
    """
    try:
        cfg = wb_settings.prompt_config(state.ACCOUNTS_DIR)
    except Exception:
        return "passthrough", ""
    mode = wb_prompt.normalize_mode(cfg.get("mode"))
    if mode == "passthrough":
        return mode, ""
    path_value = str(cfg.get("file") or "").strip() or state.PROMPT_FILE_OVERRIDE
    try:
        text = wb_prompt.load_prompt(mode, path_value)
    except Exception as exc:
        log("prompt file unreadable (%s); using the built-in prompt" % exc)
        text = wb_prompt.DEFAULT_PROMPT
    return mode, text


def prompt_cache_key_enabled():
    """Whether to inject prompt_cache_key into outbound requests.

    Off by default. The upstream turns out to cache repeated prefixes on its
    own: with an identical ~8k-token prefix sent twice, the second call already
    reports prompt_cache_hit_tokens=9600 and the same credit with or without
    this field, on both exits (www.workbuddy.ai and copilot.tencent.com) and on
    both a free and a billed model. Injecting it changed neither the hit rate
    nor the charge, so it is left as an opt-in for experimenting rather than
    added to every request.
    """
    return os.environ.get("WB_PROMPT_CACHE_KEY", "0").strip().lower() in (
        "1", "true", "yes", "on")


def inject_prompt_cache_key(body, uid, conversation):
    """Add the upstream prompt_cache_key so its prefix cache can be reused.

    Kept for experimentation only: measurement on this upstream showed the
    prefix cache working without it (see prompt_cache_key_enabled). The key
    still carries the account uid, because the upstream cache is scoped per
    account -- a key shared between accounts would let one account's request
    read another's cached prefix, so this must never be made a fixed string.

    Priority matches the client's intent: an explicit prompt_cache_key is never
    overwritten; then the body's own conversation id; then the session key the
    gateway resolved (header or conversation-prefix derived).
    """
    if not isinstance(body, dict):
        return body
    existing = body.get("prompt_cache_key")
    if isinstance(existing, str) and existing.strip():
        return body
    conv = conversation if isinstance(conversation, str) else ""
    for field in ("conversation_id", "conversationId"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            conv = value.strip()
            break
    uid8 = (uid or "")[:8] or "-"
    seed = "%s|%s" % (uid or "", conv)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    out = dict(body)
    out["prompt_cache_key"] = "wb2a-%s-%s" % (uid8, digest)
    return out


def estimate_tokens(text):
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = len(text) - len(CJK_RE.sub("", text))
    other = len(text) - cjk
    return cjk + max(1, int(other / 3.6)) if text else 0


def turn_metadata_fields(payload):
    """Flatten the request_kind / turn_trigger / thread_source hints we get.

    Codex sends them either as plain client_metadata keys or as a JSON string
    under a metadata key of its own, so both shapes are read. Returns {} when
    the payload carries none of them.
    """
    if not isinstance(payload, dict):
        return {}
    meta = payload.get("client_metadata")
    if not isinstance(meta, dict):
        return {}

    # Collect every possible source/trigger field.
    fields = {}
    for key, value in meta.items():
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                inner = json.loads(value)
            except Exception:
                inner = None
            if isinstance(inner, dict):
                for k in ("request_kind", "turn_trigger", "thread_source", "kind", "trigger", "source"):
                    if k in inner:
                        fields[k] = inner[k]
        if key in ("request_kind", "turn_trigger", "thread_source"):
            fields[key] = value
    return fields


def is_compaction_request(payload):
    """True for the operator's own "compact the context" request.

    request_kind=compaction carries the same word as the background keyword, but
    this request is one the user asked for: the client sends it on the user's
    thread, while a compaction the client started by itself names the job that
    started it. Refusing this one takes the context-compaction button away.
    """
    fields = turn_metadata_fields(payload)
    kind = str(fields.get("request_kind") or "").strip().lower()
    if "compact" not in kind:
        return False
    source = str(fields.get("thread_source") or "").strip().lower()
    return source not in BACKGROUND_THREAD_SOURCES


def background_request_reason(payload):
    """若這是 Codex 自己發的背景請求，回傳說明字串；否則回傳 ""。

    只看 client_metadata，不碰訊息內容。
    """
    fields = turn_metadata_fields(payload)
    if not fields:
        return ""

    if is_compaction_request(payload):
        return ""

    blob = " ".join(str(v) for v in fields.values()).lower()
    for kw in BACKGROUND_TRIGGER_KEYWORDS:
        if kw in blob:
            return "%s=%s" % (
                ",".join(sorted(fields.keys())),
                ",".join(str(fields[k]) for k in sorted(fields)),
            )
    return ""


def background_request_message(reason):
    return ("這是客戶端自己發的背景請求（%s），本機代理已擋下，"
            "避免在沒有實際操作時消耗上游額度。"
            "要放行請把 wb_proxy.py 的 BLOCK_BACKGROUND_REQUESTS 改成 False。"
            % reason)


def banned_model_message(model):
    allowed = "、".join(sorted(ALLOWED_MODELS))
    return ("模型 %s 已被本機代理封鎖（依 ALLOWED_MODELS / BANNED_SUBSTRING 設定）。"
            "目前允許：%s。要放行請編輯 wb_proxy.py 的 ALLOWED_MODELS。"
            % (model, allowed))


def sum_usage(total, part):
    """把一輪的 token 用量累加起來。

    代跑網路工具會多跑好幾次上游，那些 token 是真的花掉的，所以記帳要加總，
    不能讓最後一輪蓋掉前面幾輪。
    """
    if not isinstance(part, dict):
        return total
    if not isinstance(total, dict):
        total = {}
    for key, value in part.items():
        if isinstance(value, dict):
            total[key] = sum_usage(total.get(key), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = (total.get(key) or 0) + value
        elif key not in total:
            total[key] = value
    return total


def build_citations(text, sources):
    """把模型實際引用到的來源轉成 url_citation annotations。

    只標註真的有出現在工具輸出裡的網址 —— 模型自己編的連結不會被當成引用。
    """
    text = str(text or "")
    if not text or not sources:
        return []
    by_url = {}
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        if not url:
            continue
        by_url.setdefault(url, s)
        by_url.setdefault(url.rstrip("/"), s)

    anns = []
    seen = set()

    def add(url, title, start, end):
        key = (url, start, end)
        if key in seen or start < 0 or end <= start:
            return
        seen.add(key)
        anns.append({
            "type": "url_citation",
            "url": url,
            "title": title or url,
            "start_index": start,
            "end_index": end,
        })

    md_spans = []
    for m in _CITATION_MD_RE.finditer(text):
        url = m.group(2)
        src = by_url.get(url) or by_url.get(url.rstrip("/"))
        if not src:
            continue
        md_spans.append((m.start(0), m.end(0)))
        add(url, src.get("title") or m.group(1), m.start(0), m.end(0))

    for m in re.finditer(r"https?://[^\s<>()\[\]]+", text):
        if any(m.start(0) >= s and m.end(0) <= e for s, e in md_spans):
            continue
        url = m.group(0).rstrip(".,;:!?")
        src = by_url.get(url) or by_url.get(url.rstrip("/"))
        if not src:
            continue
        add(url, src.get("title"), m.start(0), m.start(0) + len(url))

    anns.sort(key=lambda a: (a["start_index"], a["end_index"]))
    return anns


def derive_affinity_key(messages):
    """Derive a stable affinity key from a conversation's stable prefix.
    The first two messages (system + first user turn) stay byte-identical for
    the whole life of a conversation, so hashing them pins every later turn of
    that conversation to the same upstream account - exactly what prompt
    caching needs. Distinct conversations differ in their first user turn and
    therefore still spread across the pool.
    """
    if not AFFINITY_BY_PREFIX:
        return None
    try:
        msgs = messages or []
        if not msgs:
            return None
        head = msgs[:2]
        blob = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "pfx-" + hashlib.sha256(blob).hexdigest()[:16]
    except Exception:
        return None


def prompt_fingerprint(messages):
    """Privacy-safe fingerprint of the outgoing prompt.
    Cache hits need a byte-identical prefix, so these hashes answer "is my
    prefix stable / is my conversation continuous?" without storing any text.
    """
    try:
        def h(obj):
            blob = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        msgs = messages or []
        out = {"msgs_sha": h(msgs), "n_msgs": len(msgs)}
        if msgs:
            out["system_sha"] = h(msgs[0]) if msgs[0].get("role") == "system" else ""
            out["prefix_sha"] = h(msgs[:-1]) if len(msgs) > 1 else ""
        return out
    except Exception:
        return {}


def local_web_tools_enabled():
    """Panel switch: does this gateway run web_search / web_fetch itself?

    Read per request, so flipping the panel takes effect on the next one
    without a restart.
    """
    try:
        return wb_settings.local_web_tools(state.ACCOUNTS_DIR) is True
    except Exception:
        return False


def web_tools_active(body):
    """True when this request's tools were swapped for the gateway's own.

    Interception only applies to a request whose definitions the gateway
    injected: with the switch off, a client's own same-named function must be
    forwarded instead of being swallowed here.
    """
    return isinstance(body, dict) and body.get("_web_tools") is True
