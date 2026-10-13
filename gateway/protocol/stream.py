"""SSE frame cleanup and stream aggregation for chat completions."""


import json
import time
import uuid
from gateway.common.ids import new_id as _new_id
from gateway.common.log import log
from gateway.protocol import state
from gateway.protocol.usage import normalize_usage_cache_aliases
from gateway.protocol.repair import is_truncated_arguments
from gateway.protocol.request import estimate_tokens

NOISE_KEYS = ("extra_fields", "refusal", "reasoning_content")


def strip_data_prefix(line):
    line = line.strip()
    # SSE comment / heartbeat / keepalive / empty line
    if not line or line.startswith(":"):
        return ""
    while line.startswith("data:"):
        line = line[5:].strip()
    # Handle possible "data: : heartbeat"
    if not line or line.startswith(":"):
        return ""
    return line


def clean_chunk(raw):
    """Drop the empty noise fields the WorkBuddy gateway pads deltas with."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    changed = False
    # #164: a real hit must not be shadowed by zero aliases on the way out
    if isinstance(obj.get("usage"), dict):
        before = dict(obj["usage"])
        normalize_usage_cache_aliases(obj["usage"])
        if obj["usage"] != before:
            changed = True
    for choice in obj.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason") == "":
            choice["finish_reason"] = None   # strict clients trip on "" (hub #296)
            changed = True
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        # Check name and arguments explicitly: an empty function_call dict is
        # truthy, so a truthiness test leaves the placeholder in place.
        fc = delta.get("function_call")
        if fc is not None:
            fc_empty = False
            if isinstance(fc, dict):
                fc_empty = not fc.get("name")
            else:
                fc_empty = not fc
            if fc_empty:
                delta.pop("function_call", None)
                changed = True
        if isinstance(delta.get("tool_calls"), list) and not delta["tool_calls"]:
            delta.pop("tool_calls")
            changed = True
        for key in NOISE_KEYS:
            if key in delta and not delta.get(key):
                delta.pop(key)
                changed = True
        if not delta and not choice.get("finish_reason"):
            return ""
    return json.dumps(obj, ensure_ascii=False) if changed else raw


def _strip_empty_fc(obj):
    """PATCHED-BY-OPS: 递归剔除空 function_call 占位（Responses/chat 通用）。"""
    changed = False
    if isinstance(obj, dict):
        fc = obj.get("function_call")
        if isinstance(fc, dict) and not fc.get("name"):
            obj.pop("function_call", None)
            changed = True
        tc = obj.get("tool_calls")
        if isinstance(tc, list) and not tc:
            obj.pop("tool_calls", None)
            changed = True
        for v in list(obj.values()):
            if _strip_empty_fc(v):
                changed = True
    elif isinstance(obj, list):
        for v in obj:
            if _strip_empty_fc(v):
                changed = True
    return changed


def clean_responses_frame(frame):
    """PATCHED-BY-OPS: 清洗 Responses SSE 帧（bytes）。
    输入 b'event: x\ndata: {...}\n\n'；只改写 data: 行的 JSON，
    event: 行原样保留。解析失败原样返回（不破坏未知格式）。
    """
    if not frame:
        return frame
    try:
        text = frame.decode("utf-8")
    except Exception:
        return frame
    out, changed = [], False
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("data:"):
            payload = st[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    if _strip_empty_fc(obj):
                        line = "data: " + json.dumps(obj, ensure_ascii=False)
                        changed = True
                except Exception:
                    pass
        out.append(line)
    return ("\n".join(out) + "\n\n").encode("utf-8") if changed else frame



def aggregate_stream(raw_iter, model, resp_id):
    """Fold an SSE stream into one non-streaming chat.completion object."""
    content, reasoning, finish = [], [], "stop"
    tool_calls_map = {}
    usage = None
    started = time.time()
    first_chunk_at = None
    saw_done = False
    for line in raw_iter:
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            if data == "[DONE]":
                saw_done = True
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if first_chunk_at is None:
            first_chunk_at = time.time()
        if chunk.get("id"):
            resp_id = chunk["id"]
        if chunk.get("model"):
            model = chunk["model"]
        u = chunk.get("usage")
        if u:
            if usage is None or (u.get("total_tokens") or 0) >= (usage.get("total_tokens") or 0):
                usage = u
                normalize_usage_cache_aliases(usage)  # #164
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls_map)
                fn = tc.get("function") or {}
                call_id = tc.get("id")
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": call_id or _new_id("call_"),
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if call_id:
                        entry["id"] = call_id
                    if fn_name:
                        entry["function"]["name"] = (entry["function"]["name"] or "") + fn_name
                    if fn_args:
                        entry["function"]["arguments"] = (entry["function"]["arguments"] or "") + fn_args
            fc = delta.get("function_call")
            # The upstream ends streams with an empty-name function_call;
            # skip it, or a fake tool_call flips finish_reason to "tool_calls"
            # and strict clients hang.
            fc_is_empty = (not isinstance(fc, dict)) or (not fc.get("name"))
            if fc and isinstance(fc, dict) and not fc_is_empty:
                idx = 0
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": _new_id("call_"),
                        "type": "function",
                        "function": {
                            "name": fc.get("name") or "",
                            "arguments": fc.get("arguments") or "",
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if fc.get("name") and not entry["function"]["name"]:
                        entry["function"]["name"] = fc["name"]
                    if fc.get("arguments"):
                        entry["function"]["arguments"] += fc["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    # Second guard: drop any tool_call with no name and no arguments, so an
    # array-shaped placeholder never reaches the client.
    if tool_calls_map:
        tool_calls_map = {
            k: v for k, v in tool_calls_map.items()
            if (v.get("function") or {}).get("name")
        }
    # A stream cut off by max_tokens / a dropped connection leaves the last
    # tool call with half-written JSON. Drop those calls instead of handing
    # the client unparsable arguments; only non-empty unparsable strings
    # count as truncated, so no-argument tools survive (#153).
    if tool_calls_map and (finish == "length" or not saw_done):
        tool_calls_map = {
            k: v for k, v in tool_calls_map.items()
            if not is_truncated_arguments((v.get("function") or {}).get("arguments"))
        }
    if tool_calls_map:
        ordered_tcs = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
        message["tool_calls"] = ordered_tcs
        if finish in ("stop", None):
            finish = "tool_calls"
    elif finish == "tool_calls":
        # All placeholders filtered, no real call: end normally so the client stops waiting.
        finish = "stop"
    if usage is None or (usage.get("total_tokens") or 0) == 0:
        full_c = "".join(content)
        full_r = "".join(reasoning)
        if full_c or full_r:
            comp = estimate_tokens(full_c) + estimate_tokens(full_r)
            prompt_est = max(1, comp // 2)
            usage = {
                "prompt_tokens": prompt_est,
                "completion_tokens": comp,
                "total_tokens": prompt_est + comp,
                "completion_tokens_details": {"reasoning_tokens": estimate_tokens(full_r)},
                "prompt_tokens_details": {"cached_tokens": 0},
            }
    out = {
        "id": resp_id or "chatcmpl-wb",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        out["usage"] = usage
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["first_chunk_at"] = first_chunk_at
    return out
