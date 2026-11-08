"""Tool-call pairing repair and reasoning backfill."""


import json
from gateway.common.log import log


def _empty_content(value):
    """True when an assistant message carries no content at all.
    None, "", and [] all count; a missed [] triggers upstream 11148."""
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def _merge_reasoning_content(dst, src):
    """Fold src's reasoning_content into dst, newline-joined when both exist."""
    rc = src.get("reasoning_content")
    if not isinstance(rc, str) or not rc:
        return
    prev = dst.get("reasoning_content")
    if isinstance(prev, str) and prev:
        dst["reasoning_content"] = prev + "\n" + rc
    else:
        dst["reasoning_content"] = rc


def merge_adjacent_tool_calls(messages):
    """Merge back-to-back assistant tool_calls messages into one.
    Clients replay a parallel batch as adjacent messages (upstream 11148)."""
    if not isinstance(messages, list) or len(messages) < 2:
        return messages, False
    out = []
    changed = False
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if m.get("role") == "assistant" and out:
            prev = out[-1] if isinstance(out[-1], dict) else None
            if prev is not None and prev.get("role") == "assistant":
                tcs = m.get("tool_calls")
                prev_calls = prev.get("tool_calls")
                # Form 1: this message declares tool calls and has no content.
                if (isinstance(tcs, list) and tcs
                        and _empty_content(m.get("content"))
                        and isinstance(prev_calls, list) and prev_calls):
                    prev["tool_calls"] = prev_calls + tcs
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue
                # Form 2: plain string content folds into a preceding
                # content-less tool_calls assistant.
                if ("tool_calls" not in m
                        and isinstance(m.get("content"), str) and m["content"]
                        and isinstance(prev_calls, list) and prev_calls
                        and _empty_content(prev.get("content"))):
                    prev["content"] = m["content"]
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True


def is_truncated_arguments(raw):
    """True when a non-empty tool-arguments string is not valid JSON.
    Empty strings are legal no-argument tools; parseable JSON is left alone."""
    if not isinstance(raw, str):
        return False
    trimmed = raw.strip()
    if not trimmed:
        return False
    try:
        json.loads(trimmed)
        return False
    except Exception:
        return True


def drop_truncated_tool_calls(calls):
    """Return the tool calls whose arguments are not half-written JSON."""
    if not isinstance(calls, list):
        return calls
    kept = []
    for call in calls:
        if not isinstance(call, dict):
            kept.append(call)
            continue
        fn = call.get("function")
        if isinstance(fn, dict) and is_truncated_arguments(fn.get("arguments")):
            continue
        kept.append(call)
    return kept


def repack_tool_result_blocks(messages):
    """Keep a tool_calls batch and its results adjacent.
    Upstream rejects any message between them (code 11148); only reorders."""
    if not isinstance(messages, list) or len(messages) < 3:
        return messages, False
    out = []
    changed = False
    i = 0
    while i < len(messages):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "assistant":
            out.append(m)
            i += 1
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            out.append(m)
            i += 1
            continue
        want = set()
        for tc in calls:
            if isinstance(tc, dict):
                tid = tc.get("id")
                if isinstance(tid, str) and tid:
                    want.add(tid)
        out.append(m)
        i += 1
        results = []
        between = []
        while i < len(messages):
            mm = messages[i]
            if not isinstance(mm, dict):
                break
            role = mm.get("role")
            if role == "tool":
                tid = mm.get("tool_call_id")
                if not (isinstance(tid, str) and tid in want):
                    break
                results.append(mm)
                i += 1
                continue
            # Any assistant message ends the batch; anything else is an
            # intruder (before or between results) moved behind the batch.
            if role == "assistant":
                break
            between.append(mm)
            i += 1
        if results:
            out.extend(results)
            out.extend(between)
            if between:
                changed = True
        else:
            out.extend(between)
    if not changed:
        return messages, False
    return out, True


def cleanup_orphan_tool_calls(messages):
    """Drop tool calls that have no result, and results that have no call.
    A never-answered entry would make upstream 11148 on every later turn."""
    if not isinstance(messages, list) or not messages:
        return messages, False
    call_ids = set()
    result_ids = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            tid = m.get("tool_call_id")
            if isinstance(tid, str) and tid:
                result_ids.add(tid)
        elif role == "assistant":
            calls = m.get("tool_calls")
            if isinstance(calls, list):
                for tc in calls:
                    if isinstance(tc, dict):
                        tid = tc.get("id")
                        if isinstance(tid, str) and tid:
                            call_ids.add(tid)
    if not call_ids and not result_ids:
        return messages, False
    keep = call_ids & result_ids
    changed = False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        kept = [tc for tc in calls
                if isinstance(tc, dict) and isinstance(tc.get("id"), str)
                and tc["id"] in keep]
        if len(kept) == len(calls):
            continue
        changed = True
        if kept:
            m["tool_calls"] = kept
        else:
            m.pop("tool_calls", None)
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if not (isinstance(tid, str) and tid in keep):
                changed = True
                continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True


def backfill_reasoning_content(messages, model, thinking_enabled=None):
    if not model or not str(model).lower().startswith("deepseek"):
        return messages
    if thinking_enabled is None:
        thinking_enabled = False
    has_trace = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        reasoning = m.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            has_trace = True
            break
        if "reasoning_content" in m:
            has_trace = True
            break
    if not thinking_enabled and not has_trace:
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            rc = item.get("reasoning_content")
            if not isinstance(rc, str):
                # Non-string (null/number/absent) counts as missing, matching
                # the official `typeof !== "string"` check.
                legacy = item.get("reasoning")
                rc = legacy if isinstance(legacy, str) else ""
                item["reasoning_content"] = rc
            # Upstream rejects empty/absent reasoning, so mirror a whitespace
            # placeholder onto `reasoning` (no model-visible semantics).
            existing = item.get("reasoning")
            if not (isinstance(existing, str) and existing):
                item["reasoning"] = rc if rc else " "
            out.append(item)
        else:
            out.append(m)
    return out


def normalize_roles(messages):
    """Map role names the upstream rejects onto ones it accepts.
    "developer" means "system" here; sent verbatim it fails with code 11128."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        item = m
        if m.get("role") == "developer":
            item = dict(m)
            item["role"] = "system"
        out.append(item)
    return out


# Empty text parts (upstream code 11151): every `type:"text"` part is length
# checked, and an empty string is a hard 400 that retires the conversation --
# every retry then replays the same bad history. The upstream also sniffs a
# string body that starts with "[" and parses it as a part list, so the same
# fix applies to sniffer-parsed strings. Fill the value, never drop the part.
EMPTY_TEXT_PART_FILL = " "


def _fill_empty_text_parts(parts):
    """Give every empty text part a placeholder; True when one was filled."""
    changed = False
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if text is None or text == "":
            part["text"] = EMPTY_TEXT_PART_FILL
            changed = True
    return changed


def _sniffed_parts(content):
    """Return the parsed parts when a string body would be read as part list."""
    if not isinstance(content, str):
        return None
    stripped = content.lstrip()
    if not stripped.startswith("["):
        return None
    try:
        parts = json.loads(stripped)
    except Exception:
        return None
    if not parts or not isinstance(parts, list):
        return None
    if not all(isinstance(part, dict) for part in parts):
        return None
    return parts


def guard_empty_text_parts(messages):
    """Fill empty text parts, in real part lists and in sniffer-parsed strings."""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            # Copy only the text parts that need changing; images and every
            # other part stay the same object.
            parts = [dict(part) if isinstance(part, dict)
                     and part.get("type") == "text" else part
                     for part in content]
            if _fill_empty_text_parts(parts):
                m["content"] = parts
        else:
            parts = _sniffed_parts(content)
            if parts is not None and _fill_empty_text_parts(parts):
                m["content"] = json.dumps(parts, ensure_ascii=False)
    return messages


# Models whose upstream reads the `reasoning` field, not `reasoning_content`.
# Matching is exact or "prefix-", so "kimiya" never matches "kimi".
MIRROR_REASONING_PREFIXES = (
    "kimi", "glm", "minimax", "mimo", "step", "space-bunny",
)


def mirror_upstream_reasoning(messages, model):
    """Mirror reasoning_content into the upstream reasoning field."""
    model_name = str(model or "").lower()
    if not any(model_name == prefix or model_name.startswith(prefix + "-")
               for prefix in MIRROR_REASONING_PREFIXES):
        return messages
    if not isinstance(messages, list):
        return messages
    out = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            out.append(message)
            continue
        item = dict(message)
        reasoning_content = item.get("reasoning_content")
        reasoning = item.get("reasoning")
        if (isinstance(reasoning_content, str) and reasoning_content.strip()
                and not (isinstance(reasoning, str) and reasoning.strip())):
            item["reasoning"] = reasoning_content
        out.append(item)
    return out
