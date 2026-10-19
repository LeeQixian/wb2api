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
    # Resolve thinking state first: with thinking on, upstream requires
    # reasoning_content on every assistant message.
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
    # system-prompt mode. passthrough keeps the client's messages (and
    # tops up a placeholder), custom replaces them, append inserts after them.
    mode, text = prompt_mode_config()
    messages = wb_prompt.apply_mode(messages, mode, text, placeholder=state.SYSTEM_PROMPT)
    body = dict(payload)
    # Private Response-path markers (namespace map, local-web-tools flag) are
    # dropped here: they are not part of the upstream protocol.
    for _marker in [k for k in body if str(k).startswith("_")]:
        body.pop(_marker, None)
    # dict(payload) copies the raw model name; overwrite it with the alias.
    body["model"] = model
    body["messages"] = messages
    # Repair tool-call pairing before the body leaves, or upstream rejects
    # every later turn. Merge split batches first, then repack.
    repaired, _merged = merge_adjacent_tool_calls(body["messages"])
    repaired, _repacked = repack_tool_result_blocks(repaired)
    repaired, _cleaned = cleanup_orphan_tool_calls(repaired)
    body["messages"] = repaired
    translate_max_completion_tokens(body)
    # Upstream GPT-family rejects max_output_tokens below 16 with a 400.
    if "max_tokens" in body and str(model).lower().startswith("gpt-"):
        try:
            value = int(body["max_tokens"])
        except (TypeError, ValueError):
            value = 0
        if 0 < value < 16:
            body["max_tokens"] = 16
    normalize_tool_choice(body)
    normalize_tools(body)
    normalize_stop(body)
    # when the client omits max_output_tokens, default to the catalog's
    # maxOutputTokens; an explicit value always wins.
    if "max_tokens" not in body:
        default_max = model_default_max_output_tokens(model)
        if default_max:
            body["max_tokens"] = default_max
    # Thinking injection for DeepSeek models: thinking.type=enabled alone yields
    # no trace, upstream needs an effort level too. The client's choice wins.
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
            # it the model emits unparsable text calls.
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


#: Schema type aliases some clients send instead of JSON Schema names.
_SCHEMA_TYPE_MAP = {
    "str": "string", "text": "string", "int": "integer", "bool": "boolean",
    "dict": "object", "list": "array", "float": "number",
}
_SCHEMA_VALID_TYPES = {"string", "number", "integer", "boolean", "array",
                       "object", "null"}
#: Null-valued metadata the upstream Go validator rejects outright.
_SCHEMA_NULL_DROP_KEYS = ("description", "title", "format", "pattern",
                          "items", "default")


def _deep_normalize_schema(s):
    """Recursively repair a JSON Schema for the upstream validator (11129/11101)."""
    if not isinstance(s, dict):
        return s
    for k in list(s.keys()):
        if s[k] is None and k in _SCHEMA_NULL_DROP_KEYS:
            s.pop(k, None)
    if "type" in s:
        typ = s["type"]
        if isinstance(typ, str):
            low = typ.strip().lower()
            if low in _SCHEMA_TYPE_MAP:
                s["type"] = _SCHEMA_TYPE_MAP[low]
            elif low not in _SCHEMA_VALID_TYPES:
                if "properties" in s:
                    s["type"] = "object"
                elif "items" in s:
                    s["type"] = "array"
                else:
                    s.pop("type", None)
            else:
                s["type"] = low
        elif isinstance(typ, list):
            s["type"] = [_SCHEMA_TYPE_MAP.get(str(t).lower(), str(t).lower())
                         for t in typ if t]
    if s.get("type") == "object" or ("properties" in s and s.get("type") is None):
        if s.get("type") is None:
            s["type"] = "object"
        if not isinstance(s.get("properties"), dict):
            s["properties"] = {}
    if "required" in s:
        req = s["required"]
        if isinstance(req, list):
            cleaned = [str(x) for x in req if x is not None and str(x).strip()]
            if cleaned:
                s["required"] = cleaned
            else:
                s.pop("required", None)
        elif isinstance(req, str) and req.strip():
            s["required"] = [req.strip()]
        else:
            s.pop("required", None)
    if "items" in s:
        items = s["items"]
        if isinstance(items, list):
            first = next((it for it in items if isinstance(it, dict)), None)
            s["items"] = _deep_normalize_schema(dict(first)) if first else {"type": "string"}
        elif isinstance(items, dict):
            _deep_normalize_schema(items)
        else:
            s.pop("items", None)
    if isinstance(s.get("properties"), dict):
        for prop_val in s["properties"].values():
            _deep_normalize_schema(prop_val)
    for def_key in ("$defs", "definitions"):
        if isinstance(s.get(def_key), dict):
            for d_val in s[def_key].values():
                _deep_normalize_schema(d_val)
    for comb_key in ("anyOf", "oneOf", "allOf"):
        if isinstance(s.get(comb_key), list):
            for sub in s[comb_key]:
                _deep_normalize_schema(sub)
    return s


def _normalize_tool_parameters(fn):
    """Repair function.parameters so upstream schema validation accepts it.
    Root must be object-typed; nested defects fixed by _deep_normalize_schema."""
    if not isinstance(fn, dict):
        return
    params = fn.get("parameters")
    if params is None or not isinstance(params, dict):
        fn["parameters"] = {"type": "object", "properties": {}}
        return
    p = dict(params)
    if p.get("type") != "object":
        p["type"] = "object"
    if "properties" not in p or not isinstance(p.get("properties"), dict):
        p["properties"] = {}
    _deep_normalize_schema(p)
    fn["parameters"] = p


def normalize_tools(obj):
    # Legacy clients may still send root-level functions instead of tools.
    if "functions" in obj and "tools" not in obj:
        funcs = obj.pop("functions")
        if isinstance(funcs, list):
            obj["tools"] = [{"type": "function", "function": f}
                            for f in funcs if isinstance(f, dict)]
    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        if "tools" in obj:
            obj.pop("tools", None)
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
            _normalize_tool_parameters(fn)
            norm.append({"type": "function", "function": fn})
        else:
            item = dict(t)
            if "function" in item and isinstance(item["function"], dict):
                fn = dict(item["function"])
                _normalize_tool_parameters(fn)
                item["function"] = fn
            norm.append(item)
    if norm:
        obj["tools"] = norm
    else:
        obj.pop("tools", None)


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
    Empty/invalid values drop the field (code 11101)."""
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
    Read per request, so set-prompt applies without a restart."""
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
    Off by default: this upstream caches prefixes without it already."""
    return os.environ.get("WB_PROMPT_CACHE_KEY", "0").strip().lower() in (
        "1", "true", "yes", "on")


def inject_prompt_cache_key(body, uid, conversation):
    """Add the upstream prompt_cache_key so its prefix cache can be reused.
    Experiment only. The key carries the uid, never a fixed string."""
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
    Codex sends them as client_metadata keys or as a JSON metadata string."""
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
    request_kind=compaction is user-driven, unlike the background keyword."""
    fields = turn_metadata_fields(payload)
    kind = str(fields.get("request_kind") or "").strip().lower()
    if "compact" not in kind:
        return False
    source = str(fields.get("thread_source") or "").strip().lower()
    return source not in BACKGROUND_THREAD_SOURCES


def background_request_reason(payload):
    """Reason string when this is Codex's own background request, else "".
    Only client_metadata is read; message content is never inspected."""
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
    """Accumulate one turn's token usage.
    Gateway-run web tools make extra upstream calls, so counts must add up."""
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
    """Turn sources the model actually cited into url_citation annotations.
    Only URLs present in tool output count; invented links never do."""
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
    It pins one conversation's turns to one account; others still spread."""
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
    Hash-only, so prefix stability can be checked without storing any text."""
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
    """Does this gateway run web_search / web_fetch itself?
    Read per request, so flipping the switch needs no restart."""
    try:
        return wb_settings.local_web_tools(state.ACCOUNTS_DIR) is True
    except Exception:
        return False


def web_tools_active(body):
    """True when this request's tools were swapped for the gateway's own.
    Only gateway-injected definitions are intercepted, never a client's own."""
    return isinstance(body, dict) and body.get("_web_tools") is True
