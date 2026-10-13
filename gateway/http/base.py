"""HTTP handler mixin: request parsing, auth, CORS and error envelopes."""

import json
import sys
import threading
import time
import urllib.error
from urllib.parse import urlparse, parse_qs

from gateway import settings, translate, upstream
from gateway.common.log import log
from gateway.http import state as _state
from gateway.http.errors import BadJSON, BodyTooLarge


class HTTPBase(object):
    """Shared plumbing; the route mixins build on this."""

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


    def _cors(self):
        if _state.ARGS is not None and getattr(_state.ARGS, "cors", False):
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
        if length > _state.MAX_PAYLOAD_BYTES:
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
        # A 429 raised without an upstream call (the pool is parked by the
        # daily token guard) carries its own text; everything else keeps the
        # upstream wording.
        custom = getattr(exc, "message", "")
        text = custom or (
            "upstream rate limit reached for this model; retry in %ds" % wait)
        detail = ""
        if exc.detail and not custom:
            detail = " - " + exc.detail[:200]
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps({
            "error": {
                "message": text + detail,
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


    def _read_chunked_body(self, max_bytes=_state.MAX_PAYLOAD_BYTES):
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


    def _read_payload(self, max_bytes=_state.MAX_PAYLOAD_BYTES):
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
                        % (exc.length, _state.MAX_PAYLOAD_BYTES), "invalid_request_error")
        except BadJSON:
            self._error(400, "invalid JSON body", "invalid_request_error")
        return None


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
        if settings.check_key(_state.ACCOUNTS_DIR, self._supplied_key()):
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
        req_realm = getattr(self, "_pinned_realm", None) or self._request_realm()
        owner = upstream.exclusive_realm(model or "")
        if req_realm in ("intl", "cn") and owner and owner != req_realm:
            served = "国内版" if owner == "cn" else "国际版"
            used = "国内版" if req_realm == "cn" else "国际版"
            return ("模型 %s 只在%s提供，但请求指定的是%s出口"
                    "（模型名前缀或 X-Realm: %s）。请改用对应出口或去掉前缀/X-Realm。"
                    % (model, served, used, req_realm))
        if upstream.is_model_banned(model or ""):
            return upstream.banned_model_message(model)
        return ""


    def do_OPTIONS(self):
        self._handle_expect_continue()
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()
