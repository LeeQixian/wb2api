"""SSE frame cleanup and stream aggregation for chat completions."""


import http.client
import json
import threading
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
    # A real cache hit must not be shadowed by zero aliases on the way out.
    if isinstance(obj.get("usage"), dict):
        before = dict(obj["usage"])
        normalize_usage_cache_aliases(obj["usage"])
        if obj["usage"] != before:
            changed = True
    for choice in obj.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason") == "":
            choice["finish_reason"] = None   # strict clients trip on ""
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
    """Recursively drop empty function_call placeholders."""
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
    """Clean one Responses SSE frame (bytes).
    Only data: line JSON is rewritten; unparsable frames pass through."""
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



SSE_HEARTBEAT_SECONDS = 15.0


class SSEHeartbeat:
    """Write SSE comments while the request thread is blocked on the upstream.
    Shares the request's write lock so a comment can never split a frame."""

    def __init__(self, write, interval=SSE_HEARTBEAT_SECONDS):
        self._write = write
        self._interval = max(0.05, float(interval))
        self._lock = threading.RLock()
        self._last_write_at = time.monotonic()
        self._stop = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="wb-sse-heartbeat", daemon=True)
        self._thread.start()

    def write(self, data):
        if not data:
            return
        with self._lock:
            if self._closed:
                return
            self._write(data)
            self._last_write_at = time.monotonic()

    def _run(self):
        tick = min(0.1, self._interval / 4.0)
        while not self._stop.wait(tick):
            with self._lock:
                if self._closed:
                    return
                if time.monotonic() - self._last_write_at < self._interval:
                    continue
                try:
                    self._write(b": keepalive\n\n")
                    self._last_write_at = time.monotonic()
                except Exception:
                    self._closed = True
                    return

    def close(self):
        with self._lock:
            self._closed = True
        self._stop.set()
        self._thread.join(timeout=max(0.1, self._interval))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def iter_sse_lines(source, block_size=8192):
    """Yield lines from an HTTP response; raise IncompleteRead on a truncated EOF."""
    read1 = getattr(source, "read1", None)
    if read1 is None:
        for line in source:
            yield line
        return
    buf = b""
    while True:
        chunk = read1(block_size)
        if not chunk:
            # read1() returns b"" when the peer closes early, even with a
            # Content-Length still outstanding, so a cut stream looks clean.
            remaining = getattr(source, "length", 0) or 0
            if remaining > 0:
                raise http.client.IncompleteRead(b"", remaining)
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line + b"\n"
    if buf:
        yield buf


def chat_usage_chunk(model, usage, chunk_id=None):
    """Trailing include_usage frame: empty choices, usage only."""
    normalize_usage_cache_aliases(usage)
    return json.dumps({
        "id": chunk_id or new_chunk_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": usage,
    }, ensure_ascii=False)


def new_chunk_id():
    """A unique chatcmpl id, so two replies never share one."""
    return "chatcmpl-wb-" + uuid.uuid4().hex[:16]


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
                normalize_usage_cache_aliases(usage)
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
            # skipping it keeps finish_reason from flipping to "tool_calls".
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
    # A truncated stream leaves the last tool call with half-written JSON;
    # drop it rather than hand the client unparsable arguments.
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
        "id": resp_id or new_chunk_id(),
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
