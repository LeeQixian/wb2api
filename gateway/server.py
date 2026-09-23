#!/usr/bin/env python3
"""wb2api — minimal local WorkBuddy -> OpenAI-compatible gateway.

Slimmed from workbuddy2api-hub: no dashboard, no panel password, no Windows
credential scanning, no LAN mode, no multi-key management. Routes:

    GET  /health              free   (launcher liveness probe)
    GET  /realm               free
    GET  /v1/models           free   (model discovery needs no key)
    GET  /credits             free   (account credit balances)
    POST /v1/chat/completions key if one is configured
    POST /v1/responses        key if one is configured

Key handling differences from the upstream project (deliberate):
  - the API key is ONLY read from the Authorization / x-api-key / api-key
    headers. The ?key= query spelling was removed: it leaks the key into
    browser history, Referer headers and proxy logs.
  - binds 127.0.0.1 by default; CORS stays off unless --cors is passed.

Standard library only (Python >= 3.9).
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

if __package__ in (None, ""):  # allow `python3 gateway/server.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import settings, translate, upstream
from gateway.accounts import AccountPool

MAX_PAYLOAD_BYTES = int(os.environ.get("WB_MAX_PAYLOAD_BYTES", 50 * 1024 * 1024))
MAX_CONCURRENT_CHAT = int(os.environ.get("WB_MAX_CONCURRENT_CHAT", 8))
CHAT_SLOT_WAIT_SECONDS = float(os.environ.get("WB_CHAT_SLOT_WAIT", 30))
POOL_REFRESH_SECONDS = 60.0

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ACCOUNTS_DIR = os.environ.get("WB_ACCOUNTS_DIR") or \
    os.path.join(os.path.dirname(CURRENT_DIR), "accounts")
DEFAULT_USAGE_DIR = os.environ.get("WB_USAGE_DIR") or \
    os.path.join(os.path.dirname(CURRENT_DIR), "usage")

# Populated by main(); tests may set them directly.
ARGS = None
POOL = None
_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CHAT)
_pool_lock = threading.Lock()
_log_lock = threading.Lock()


def log(msg, level="INFO"):
    with _log_lock:
        stream = sys.stderr if level in ("WARN", "ERROR") else sys.stdout
        print("%s [%s] %s" % (time.strftime("%H:%M:%S"), level, msg),
              file=stream, flush=True)


def refresh_pool(force=False):
    """Rescan accounts/ so `wbctl login` adds apply without a restart."""
    global POOL
    if POOL is None:
        return None
    now = time.time()
    if not force and now - getattr(POOL, "loaded_at", 0.0) < POOL_REFRESH_SECONDS:
        return POOL
    with _pool_lock:
        if not force and now - getattr(POOL, "loaded_at", 0.0) < POOL_REFRESH_SECONDS:
            return POOL
        try:
            POOL.load()
        except Exception as exc:
            log("pool reload failed: %s" % exc, "WARN")
    return POOL


class BodyTooLarge(Exception):
    def __init__(self, length):
        super().__init__(length)
        self.length = length


class BadJSON(Exception):
    pass


def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id")
        or headers.get("Conversation-Id")
        or headers.get("X-Session-Id")
        or headers.get("Session-Id")
        or payload.get("conversation_id")
        or payload.get("session_id")
        or (payload.get("metadata") or {}).get("conversation_id")
    )
    return str(key).strip() if key else None


def record_row(row):
    """Append one usage line. Errors must never break the served response."""
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        row = dict(row)
        row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with open(os.path.join(USAGE_DIR, "usage.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        log("usage write failed: %s" % exc, "WARN")


def _usage_fields(usage):
    if not usage:
        return {}
    d = {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
    }
    comp = usage.get("completion_tokens_details") or {}
    prompt = usage.get("prompt_tokens_details") or {}
    if comp.get("reasoning_tokens"):
        d["reasoning_tokens"] = comp["reasoning_tokens"]
    if prompt.get("cached_tokens"):
        d["cached_tokens"] = prompt["cached_tokens"]
    for key in ("credit", "credits", "cost"):
        if usage.get(key) is not None:
            d["credit"] = usage[key]
            break
    return d


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wb2api/1.0"
    _body_consumed = False

    def handle_one_request(self):
        # Reset per-request state: keep-alive reuses one Handler instance for
        # many requests, and a stale _body_consumed would skip a needed drain
        # and desync the connection (the bogus "414" bug of the original).
        self._body_consumed = False
        super().handle_one_request()

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def log_message(self, fmt, *args):
        try:
            status = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 200
            if status < 400 and getattr(self, "command", "GET") == "GET":
                req_path = (getattr(self, "path", "") or "").split("?")[0]
                if req_path in ("/health", "/realm", "/v1/models", "/models",
                                "/credits", "/v1/credits", "/favicon.ico"):
                    return
        except Exception:
            pass
        log(fmt % args)

    # ---------------- low-level response helpers ----------------
    def _cors(self):
        if ARGS is not None and getattr(ARGS, "cors", False):
            self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _handle_expect_continue(self):
        headers = getattr(self, "headers", None)
        if headers is None or "100-continue" not in (headers.get("Expect") or "").lower():
            return
        try:
            self.send_response_only(100)
            self.end_headers()
            self.wfile.flush()
        except Exception:
            pass

    def _drain_chunked_body(self):
        try:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    return
                size_field = line.split(b";", 1)[0].strip()
                if not size_field:
                    continue
                size = int(size_field, 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            return
                remaining = size
                while remaining > 0:
                    data = self.rfile.read(min(remaining, 65536))
                    if not data:
                        return
                    remaining -= len(data)
                self.rfile.read(2)
        except Exception:
            self.close_connection = True

    def _discard_body(self):
        """Drain the body so keep-alive stays in sync after early rejects."""
        if self._body_consumed:
            return
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        if "chunked" in (headers.get("Transfer-Encoding") or "").lower():
            self._drain_chunked_body()
            return
        try:
            length = int(headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return
        if length > MAX_PAYLOAD_BYTES:
            self.close_connection = True
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception:
            pass

    def _error(self, code, message, err_type="server_error"):
        self._handle_expect_continue()
        self._discard_body()
        self._json(code, {"error": {"message": message, "type": err_type, "code": code}})

    def _rate_limited(self, exc):
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps({
            "error": {
                "message": ("upstream rate limit reached for this model; retry in %ds"
                            % wait) + ((" - " + exc.detail[:200]) if getattr(exc, "detail", "") else ""),
                "type": "rate_limit_error",
                "code": 429,
                "retry_after": wait,
            }
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ---------------- request body ----------------
    def _read_chunked_body(self, max_bytes=MAX_PAYLOAD_BYTES):
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            size_field = line.split(b";", 1)[0].strip()
            if not size_field:
                continue
            try:
                size = int(size_field, 16)
            except ValueError:
                raise BadJSON()
            if size == 0:
                while True:
                    trailer = self.rfile.readline(65536)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > max_bytes:
                self._drain_chunked_body()
                raise BodyTooLarge(total)
            remaining = size
            while remaining > 0:
                data = self.rfile.read(min(remaining, 65536))
                if not data:
                    raise BadJSON()
                chunks.append(data)
                remaining -= len(data)
            self.rfile.read(2)
        self._body_consumed = True
        return b"".join(chunks)

    def _read_payload(self, max_bytes=MAX_PAYLOAD_BYTES):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raw = self._read_chunked_body(max_bytes=max_bytes)
        else:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                raise BadJSON()
            if length > max_bytes:
                raise BodyTooLarge(length)
            if length < 0:
                raise BadJSON()
            raw = self.rfile.read(length) if length else b"{}"
            self._body_consumed = True
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            raise BadJSON()
        return data if isinstance(data, dict) else {}

    def _payload_or_error(self):
        try:
            return self._read_payload()
        except BodyTooLarge as exc:
            self._error(413, "payload too large (%d bytes > %d limit)"
                        % (exc.length, MAX_PAYLOAD_BYTES), "invalid_request_error")
        except BadJSON:
            self._error(400, "invalid JSON body", "invalid_request_error")
        return None

    # ---------------- auth ----------------
    def _supplied_key(self):
        """Bearer / x-api-key / api-key headers only. No ?key= by design."""
        header = (self.headers.get("Authorization") or "").strip()
        supplied = ""
        if header:
            scheme, _, value = header.partition(" ")
            supplied = value.strip() if scheme.lower() == "bearer" else header
        if len(supplied) >= 2 and supplied[0] == supplied[-1] and supplied[0] in "\"'":
            supplied = supplied[1:-1].strip()
        if supplied:
            return supplied
        for name in ("x-api-key", "api-key"):
            value = (self.headers.get(name) or "").strip()
            if value:
                return value
        return ""

    def _authorized(self):
        if settings.check_key(ACCOUNTS_DIR, self._supplied_key()):
            return True
        self._error(401,
                    "invalid api key - send it as 'Authorization: Bearer <key>' "
                    "(set it with: python3 -m gateway.wbctl set-key <key>)",
                    "invalid_request_error")
        return False

    def _request_realm(self):
        header = self.headers.get("X-Realm")
        if header:
            return header.strip().lower()
        try:
            q = parse_qs(urlparse(self.path).query).get("realm", [None])[0]
            return q.strip().lower() if q else None
        except Exception:
            return None

    def _realm_guard(self, model):
        """Return an error string for realm/banned mismatches, '' when fine.

        Only an *explicit* per-request realm (X-Realm header / ?realm=) can
        conflict with a model's exclusive exit. Without one the request
        auto-routes: exclusive models pick their own exit, dual-exit models
        follow the global realm.
        """
        req_realm = self._request_realm()
        owner = upstream.exclusive_realm(model or "")
        if req_realm in ("intl", "cn") and owner and owner != req_realm:
            served = "国内版" if owner == "cn" else "国际版"
            used = "国内版" if req_realm == "cn" else "国际版"
            return ("模型 %s 只在%s提供，但请求指定的是%s出口（X-Realm: %s）。"
                    "请改用对应出口或去掉 X-Realm。" % (model, served, used, req_realm))
        if upstream.is_model_banned(model or ""):
            return upstream.banned_model_message(model)
        return ""

    # ---------------- GET ----------------
    def do_OPTIONS(self):
        self._handle_expect_continue()
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._get_health()
        if path == "/realm":
            return self._json(200, {"current": upstream.get_realm(),
                                    "options": ["intl", "cn"]})
        if path in ("/v1/models", "/models"):
            return self._get_v1_models()
        if path in ("/credits", "/v1/credits"):
            return self._get_credits()
        return self._error(404, "not found", "invalid_request_error")

    def _get_health(self):
        refresh_pool()
        info = {
            "ok": True,
            "realm": upstream.get_realm(),
            "accounts": len(POOL.accounts) if POOL else 0,
            "accounts_ready": POOL.count_ready() if POOL else 0,
            "api_key_required": bool(settings.get_api_key(ACCOUNTS_DIR)),
        }
        rep = POOL.representative() if POOL else None
        if rep is not None:
            info.update({
                "uid": rep.uid,
                "expires_at": getattr(rep, "expires_at", None),
                "credential_file": os.path.basename(rep.path or "") if rep.path else None,
            })
        return self._json(200, info)

    def _get_v1_models(self):
        req_realm = self._request_realm() or upstream.get_realm()
        try:
            entries = upstream.fetch_models(realm=req_realm, pool=refresh_pool())
            data = [upstream.model_entry(mid, meta) for mid, meta in entries]
        except Exception as exc:
            return self._error(502, str(exc))
        return self._json(200, {"object": "list", "data": data, "realm": req_realm})

    def _get_credits(self):
        refresh_pool()
        out = []
        for a in list(POOL.accounts):
            row = {"uid": a.uid, "realm": a.realm,
                   "nickname": getattr(a, "nickname", "") or "",
                   "enabled": a.enabled,
                   "expires_at": getattr(a, "expires_at", None)}
            try:
                res = a.fetch_credits()
                if isinstance(res, dict):
                    row["credits"] = res
            except Exception as exc:
                row["credits_error"] = str(exc)[:160]
            out.append(row)
        return self._json(200, {"accounts": out})

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/realm", "/realm/set"):
            if not self._authorized():
                return
            payload = self._payload_or_error()
            if payload is None:
                return
            realm = str(payload.get("realm") or "").strip().lower()
            if realm not in ("intl", "cn"):
                return self._error(400, "realm must be 'intl' or 'cn'",
                                   "invalid_request_error")
            upstream.save_persisted_realm(ACCOUNTS_DIR, realm)
            upstream.set_realm(realm)
            return self._json(200, {"current": realm})
        if path not in ("/v1/chat/completions", "/chat/completions",
                        "/v1/completions", "/completions",
                        "/v1/responses", "/responses"):
            return self._error(404, "not found", "invalid_request_error")
        if not self._authorized():
            return
        payload = self._payload_or_error()
        if payload is None:
            return
        # Upstream chat calls can hold this thread for up to 600s; bound the
        # in-flight count so a few slow clients cannot exhaust the pool.
        if not _chat_slots.acquire(timeout=CHAT_SLOT_WAIT_SECONDS):
            return self._error(503, "gateway is at its concurrent chat limit "
                                    "(%d in flight); retry shortly" % MAX_CONCURRENT_CHAT)
        try:
            if path in ("/v1/responses", "/responses"):
                return self._handle_responses(payload)
            return self._dispatch_chat_post(payload)
        finally:
            _chat_slots.release()

    def _dispatch_chat_post(self, payload):
        model = payload.get("model") or "hy4-preview"
        blocked = self._realm_guard(model)
        if blocked:
            return self._error(400, blocked, "invalid_request_error")
        forwarded = translate.build_upstream_body(payload)
        log("chat: model=%s stream=%s msgs=%d"
            % (model, bool(payload.get("stream")),
               len(forwarded.get("messages") or [])))
        session_key = extract_session_key(self.headers, payload)
        fp = translate.prompt_fingerprint(forwarded.get("messages"))
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        try:
            upstream_resp, account = self.open_upstream(payload, session_key)
        except Exception as exc:
            return self._upstream_error(model, exc, t_start, fp)
        with upstream_resp:
            if want_stream:
                return self._stream_chat(upstream_resp, model, fp, account, t_start)
            return self._nonstream_chat(upstream_resp, model, fp, account, t_start)

    def _handle_responses(self, payload):
        model = payload.get("model") or "deepseek-v4.1-flash"
        if payload.get("previous_response_id"):
            self._handle_expect_continue()
            self._discard_body()
            return self._json(400, {"error": {
                "message": "previous_response_id is not supported: this gateway "
                           "stores no response state. Send the full conversation "
                           "in 'input' instead.",
                "type": "invalid_request_error", "code": 400}})
        blocked = self._realm_guard(model)
        if blocked:
            return self._error(400, blocked, "invalid_request_error")
        session_key = extract_session_key(self.headers, payload)
        custom_names = translate.custom_tool_names(payload.get("tools"))
        chat_req = translate.responses_to_chat(payload)
        ns_map = chat_req.pop("_namespace_map", None)
        request_meta = {
            "tools": payload.get("tools") or [],
            "tool_choice": payload.get("tool_choice", "auto"),
            "parallel_tool_calls": payload.get("parallel_tool_calls", True),
        }
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        fp = translate.prompt_fingerprint(chat_req.get("messages"))
        log("responses: model=%s stream=%s msgs=%d custom_tools=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               sorted(custom_names) or "-"))
        try:
            upstream_resp, account = self.open_upstream(chat_req, session_key)
        except Exception as exc:
            return self._upstream_error(model, exc, t_start, fp)
        with upstream_resp:
            if want_stream:
                return self._stream_responses(upstream_resp, model, custom_names,
                                              request_meta, fp, account, t_start, ns_map)
            return self._nonstream_responses(upstream_resp, model, custom_names,
                                             request_meta, fp, account, t_start, ns_map)

    def open_upstream(self, payload, session_key):
        # None when the caller did not pin a realm: upstream then routes by
        # model (exclusive models to their own exit, shared ones to the
        # global realm).
        return upstream.open_upstream(POOL, payload, session_key=session_key,
                                      target_realm=self._request_realm(),
                                      log=log)

    def _upstream_error(self, model, exc, t_start, fp):
        wall = int((time.time() - t_start) * 1000)
        account_uid = getattr(exc, "account_uid", None)
        if isinstance(exc, upstream.ContentRejected):
            record_row({"model": model, "outcome": "error", "status": 403,
                        "error": exc.detail[:200], "elapsed_ms": wall,
                        "account": account_uid})
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        if isinstance(exc, upstream.RateLimited):
            record_row({"model": model, "outcome": "error", "status": 429,
                        "error": exc.detail[:200], "elapsed_ms": wall,
                        "account": account_uid})
            return self._rate_limited(exc)
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read(600).decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            record_row({"model": model, "outcome": "error", "status": exc.code,
                        "error": detail[:200], "elapsed_ms": wall, "account": account_uid})
            return self._error(exc.code, "upstream %s: %s" % (exc.code, detail))
        message = str(exc)
        record_row({"model": model, "outcome": "error", "status": 502,
                    "error": message[:200], "elapsed_ms": wall, "account": account_uid})
        if message.startswith("no usable account"):
            return self._error(503, message + " - add one with: python3 -m gateway.wbctl login")
        return self._error(502, "upstream unreachable: %s" % message)

    # ---------------- streaming / non-streaming writers ----------------
    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()

    def _stream_chat(self, upstream_resp, model, fp, account, t_start):
        self._start_sse()
        emitted = False
        last_usage = None
        first_ms = None
        streamed_text = []
        try:
            for line in upstream_resp:
                data = translate.strip_data_prefix(line.decode("utf-8", "replace"))
                if not data or data == "[DONE]" or data.startswith(":"):
                    continue
                try:
                    maybe = json.loads(data)
                    u = maybe.get("usage")
                    if u and (last_usage is None or
                              (u.get("total_tokens") or 0) >= (last_usage.get("total_tokens") or 0)):
                        last_usage = u
                    for ch in (maybe.get("choices") or []):
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            streamed_text.append(delta["content"])
                        if delta.get("reasoning_content"):
                            streamed_text.append(delta["reasoning_content"])
                except Exception:
                    pass
                cleaned = translate.clean_chunk(data)
                if not cleaned:
                    continue
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                emitted = True
                self.wfile.write(("data: %s\n\n" % cleaned).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            record_row({"model": model, "outcome": "client_aborted", "status": 200,
                        "stream": True, "elapsed_ms": wall, "ttft_ms": first_ms,
                        "account": account.uid, "fp": fp, **_usage_fields(last_usage)})
            return
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            log("stream aborted: %s" % exc, "WARN")
            record_row({"model": model, "outcome": "upstream_aborted", "status": 502,
                        "error": str(exc)[:200], "stream": True, "elapsed_ms": wall,
                        "ttft_ms": first_ms, "account": account.uid, "fp": fp,
                        **_usage_fields(last_usage)})
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        if not emitted:
            err = json.dumps({"error": {"message": "empty upstream stream",
                                        "type": "server_error"}})
            self.wfile.write(("data: %s\n\n" % err).encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        wall = int((time.time() - t_start) * 1000)
        if last_usage is None or (last_usage.get("total_tokens") or 0) == 0:
            comp = translate.estimate_tokens("".join(streamed_text))
            if comp:
                last_usage = {"prompt_tokens": max(1, comp // 2),
                              "completion_tokens": comp,
                              "total_tokens": max(1, comp // 2) + comp}
        record_row({"model": model, "outcome": "ok", "status": 200, "stream": True,
                    "elapsed_ms": wall, "ttft_ms": first_ms, "gen_ms":
                    (wall - first_ms) if first_ms is not None else None,
                    "account": account.uid, "fp": fp, **_usage_fields(last_usage)})

    def _nonstream_chat(self, upstream_resp, model, fp, account, t_start):
        try:
            result = translate.aggregate_stream(upstream_resp, model, None)
        except Exception as exc:
            record_row({"model": model, "outcome": "error", "status": 502,
                        "error": str(exc)[:200],
                        "elapsed_ms": int((time.time() - t_start) * 1000),
                        "account": account.uid, "fp": fp})
            return self._error(502, "upstream stream error: %s" % exc)
        wall = int((time.time() - t_start) * 1000)
        first_at = result.pop("first_chunk_at", None)
        result.pop("elapsed_ms", None)
        record_row({"model": model, "outcome": "ok", "status": 200, "stream": False,
                    "elapsed_ms": wall,
                    "ttft_ms": int((first_at - t_start) * 1000) if first_at else None,
                    "account": account.uid, "fp": fp, **_usage_fields(result.get("usage"))})
        return self._json(200, result)

    def _stream_responses(self, upstream_resp, model, custom_names, request_meta,
                          fp, account, t_start, ns_map):
        self._start_sse()
        holder = {"usage": None, "custom_names": custom_names,
                  "request_meta": request_meta, "namespace_map": ns_map}
        first_ms = None
        try:
            for frame in translate.stream_responses_events(upstream_resp, model, holder):
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                self.wfile.write(translate.clean_responses_frame(frame))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            record_row({"model": model, "outcome": "client_aborted", "status": 200,
                        "stream": True, "elapsed_ms": int((time.time() - t_start) * 1000),
                        "ttft_ms": first_ms, "account": account.uid, "fp": fp,
                        **_usage_fields(holder.get("usage"))})
            return
        except Exception as exc:
            log("responses stream aborted: %s" % exc, "WARN")
            record_row({"model": model, "outcome": "upstream_aborted", "status": 502,
                        "error": str(exc)[:200], "stream": True,
                        "elapsed_ms": int((time.time() - t_start) * 1000),
                        "ttft_ms": first_ms, "account": account.uid, "fp": fp,
                        **_usage_fields(holder.get("usage"))})
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        record_row({"model": model, "outcome": "ok", "status": 200, "stream": True,
                    "elapsed_ms": int((time.time() - t_start) * 1000),
                    "ttft_ms": first_ms, "account": account.uid, "fp": fp,
                    **_usage_fields(holder.get("usage"))})

    def _nonstream_responses(self, upstream_resp, model, custom_names, request_meta,
                             fp, account, t_start, ns_map):
        try:
            chat_obj = translate.aggregate_stream(upstream_resp, model, None)
        except Exception as exc:
            record_row({"model": model, "outcome": "error", "status": 502,
                        "error": str(exc)[:200],
                        "elapsed_ms": int((time.time() - t_start) * 1000),
                        "account": account.uid, "fp": fp})
            return self._error(502, "upstream stream error: %s" % exc)
        wall = int((time.time() - t_start) * 1000)
        chat_obj.pop("first_chunk_at", None)
        chat_obj.pop("elapsed_ms", None)
        result = translate.chat_to_response(chat_obj, model, custom_names,
                                            request_meta, ns_map)
        record_row({"model": model, "outcome": "ok", "status": 200, "stream": False,
                    "elapsed_ms": wall, "account": account.uid, "fp": fp,
                    **_usage_fields(chat_obj.get("usage"))})
        return self._json(200, result)


# Module paths the handler uses; set in main(), overridable in tests.
ACCOUNTS_DIR = DEFAULT_ACCOUNTS_DIR
USAGE_DIR = DEFAULT_USAGE_DIR


def build_server(host, port, handler=Handler):
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="wb2api minimal WorkBuddy -> OpenAI gateway (no dashboard)")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8788"))
    ap.add_argument("--api-key", default=None,
                    help="persist to accounts/settings.json (single key; empty keeps auth off)")
    ap.add_argument("--accounts-dir", default=ACCOUNTS_DIR)
    ap.add_argument("--usage-dir", default=USAGE_DIR)
    ap.add_argument("--realm", choices=("intl", "cn"), default=None,
                    help="initial upstream exit (default: persisted or 'intl')")
    ap.add_argument("--system-prompt", default=translate.DEFAULT_SYSTEM_PROMPT)
    ap.add_argument("--cors", action="store_true",
                    help="send Access-Control-Allow-Origin: * on API routes")
    ap.add_argument("--tasks", action="store_true",
                    help="enable the background scheduler (checkin/cat-travel/keepalive)")
    return ap.parse_args(argv)


def main(argv=None):
    global ARGS, POOL, ACCOUNTS_DIR, USAGE_DIR
    args = _parse_args(argv)
    ARGS = args
    ACCOUNTS_DIR = args.accounts_dir
    USAGE_DIR = args.usage_dir
    translate.set_system_prompt(args.system_prompt)
    upstream.set_logger(log)

    POOL = AccountPool(ACCOUNTS_DIR, log=log)
    POOL.load()

    if args.api_key is not None:
        if args.api_key:
            settings.set_api_key(ACCOUNTS_DIR, args.api_key)
        else:
            settings.clear_api_key(ACCOUNTS_DIR)

    persisted = upstream.load_persisted_realm(ACCOUNTS_DIR)
    realm = args.realm or persisted or os.environ.get("WB_PROXY_DEFAULT_REALM") or "intl"
    upstream.set_realm(realm)
    if persisted != realm:
        upstream.save_persisted_realm(ACCOUNTS_DIR, realm)

    scheduler = None
    if args.tasks:
        from gateway import scheduler as scheduler_mod
        scheduler = scheduler_mod.Scheduler(POOL, log=log, enabled=True)
        scheduler.start()

    httpd = build_server(args.host, args.port)
    display = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    log("wb2api gateway on http://%s:%d  (realm=%s, accounts=%d/%d ready, "
        "api key %s, tasks %s)"
        % (display, args.port, realm,
           POOL.count_ready(), len(POOL.accounts),
           "on" if settings.get_api_key(ACCOUNTS_DIR) else "OFF (open locally)",
           "on" if args.tasks else "off"))
    log("OpenAI base URL: http://%s:%d/v1" % (display, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
