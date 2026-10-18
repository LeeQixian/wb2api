"""Integration tests for gateway.server against a fake upstream.
Fully offline: the real HTTP server is exercised against a local fake."""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import server as srv  # noqa: E402

SSE_LINES = [
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"hy3",'
    b'"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"hy3",'
    b'"choices":[{"index":0,"delta":{"content":"he"}}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"hy3",'
    b'"choices":[{"index":0,"delta":{"content":"llo"}}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"hy3",'
    b'"choices":[{"index":0,"delta":{"content":"","function_call":{"name":"","arguments":""}}}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"hy3",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n',
    b'data: [DONE]\n\n',
]


class FakeResponse:
    """Acts like urllib's HTTPResponse: iterable of raw SSE lines + ctx mgr."""

    def __init__(self, lines=None, error=None):
        self._lines = lines if lines is not None else SSE_LINES
        self._error = error

    def __enter__(self):
        if self._error:
            raise self._error
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


class FakeAccount:
    uid = "uid-fake-1"
    realm = "intl"
    enabled = True
    path = None
    nickname = ""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def fetch_credits(self):
        return {"credits": 123}


class FakePool:
    def __init__(self, accounts=None):
        self.accounts = accounts if accounts is not None else [FakeAccount()]

    def load(self):
        pass

    def count_ready(self, realm=None, model=None):
        return len(self.accounts)

    def representative(self, realm=None):
        return self.accounts[0] if self.accounts else None


class ServerCase(unittest.TestCase):
    def setUp(self):
        self._patches = []  # per-instance: a class-level list leaks mocks
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = os.path.join(self.tmp.name, "accounts")
        self.usage = os.path.join(self.tmp.name, "usage")
        os.makedirs(self.accounts)
        # server.py reads module-level ACCOUNTS_DIR/USAGE_DIR at call time.
        srv.ACCOUNTS_DIR = self.accounts
        srv.USAGE_DIR = self.usage
        srv.POOL = FakePool()
        # Default: auth off, no realm guard, open_upstream -> fake SSE.
        self.calls = []
        self.realms = []
        self.open_result = ("ok", None)
        self._saved = {k: getattr(srv, k) for k in ("ARGS",)}
        srv.ARGS = srv._parse_args([])
        self._patch("check_key", lambda d, s: True)
        self._patch_upstream("open_upstream", self._fake_open)
        self._patch_upstream("get_realm", lambda: "intl")
        self._patch_upstream("set_realm", lambda r: None)
        self._patch_upstream("save_persisted_realm", lambda d, r: None)
        self._patch_upstream("exclusive_realm", lambda m: "")
        self._patch_upstream("is_model_banned", lambda m: False)
        self._patch_upstream("banned_model_message", lambda m: "")
        self._patch_upstream("fetch_models",
                             lambda realm=None, pool=None: [("hy3", {"name": "HY3"})])
        self._patch_upstream("fetch_models_all", lambda pool=None: [
            ("hy3", {"name": "HY3", "exitRealm": "intl"}),
            ("cn:hy3", {"name": "HY3", "exitRealm": "cn"}),
            ("global:hy3", {"name": "HY3", "exitRealm": "intl"}),
        ])
        self._patch_upstream("model_entry", lambda mid, meta: {
            "id": mid, "object": "model", "owned_by": "workbuddy"})
        self.httpd = srv.build_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        for name, (target, original) in reversed(self._patches):
            setattr(target, name, original)
        self._patches = []
        self.tmp.cleanup()


    def _patch(self, name, value):
        target = srv.settings
        self._patches.append((name, (target, getattr(target, name))))
        setattr(target, name, value)

    def _patch_upstream(self, name, value):
        target = srv.upstream
        self._patches.append((name, (target, getattr(target, name))))
        setattr(target, name, value)

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _request(self, path, body=None, headers=None, method=None):
        data = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        req = urllib.request.Request(self._url(path), data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _post_chat(self, body=None, headers=None):
        return self._request("/v1/chat/completions",
                             body or {"model": "hy3", "messages":
                                      [{"role": "user", "content": "hi"}]},
                             headers)

    def _fake_open(self, pool, payload, session_key=None, target_realm=None,
                    log=None, prebuilt_body=None):
        self.calls.append(payload)
        self.realms.append(target_realm)
        kind, err = self.open_result
        if err is not None:
            raise err
        return FakeResponse(), FakeAccount()


class TestFreeRoutes(ServerCase):
    def test_health_reflects_pool(self):
        status, raw = self._request("/health")
        info = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(info["ok"])
        self.assertEqual(info["accounts_ready"], 1)
        self.assertEqual(info["realm"], "intl")

    def test_models_no_key_required(self):
        # free model discovery must answer even when auth is configured
        self._patch("check_key", lambda d, s: s == "secret")
        status, raw = self._request("/v1/models")
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(data["data"][0]["id"], "hy3")

    def test_unknown_route_404(self):
        status, _ = self._request("/accounts")
        self.assertEqual(status, 404)

    def test_credits_lists_accounts(self):
        status, raw = self._request("/credits")
        info = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(info["accounts"][0]["uid"], "uid-fake-1")


class TestChatRoutes(ServerCase):
    def test_nonstream_aggregates_sse(self):
        status, raw = self._post_chat()
        obj = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(obj["choices"][0]["message"]["content"], "hello")
        self.assertEqual(obj["choices"][0]["finish_reason"], "stop")
        self.assertNotIn("tool_calls", obj["choices"][0]["message"])
        self.assertEqual(obj["usage"]["total_tokens"], 7)
        self.assertNotIn("elapsed_ms", obj)
        self.assertNotIn("first_chunk_at", obj)

    def test_stream_forwards_cleaned_chunks(self):
        body = {"model": "hy3", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
        status, raw = self._post_chat(body)
        text = raw.decode("utf-8")
        self.assertEqual(status, 200)
        frames = [ln for ln in text.split("\n\n") if ln.startswith("data: ")]
        # role frame + "he" + "llo" + finish frame, then [DONE]
        self.assertEqual(frames[-1], "data: [DONE]")
        contents = []
        for ln in frames[:-1]:
            obj = json.loads(ln[len("data: "):])
            delta = obj["choices"][0].get("delta") or {}
            if delta.get("content"):
                contents.append(delta["content"])
            self.assertNotIn("function_call", json.dumps(obj))
        self.assertEqual("".join(contents), "hello")

    def test_bad_json_400(self):
        status, raw = self._request("/v1/chat/completions", body=b"{nope")
        self.assertEqual(status, 400)
        self.assertIn("invalid JSON", json.loads(raw)["error"]["message"])

    def test_chunked_body_accepted(self):
        # no Content-Length -> urllib uses chunked; server must read it
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"model": "hy3",
                           "messages": [{"role": "user", "content": "hi"}]})
        payload = b""
        for i in range(0, len(body), 7):
            piece = body[i:i + 7].encode()
            payload += b"%x\r\n%s\r\n" % (len(piece), piece)
        payload += b"0\r\n\r\n"
        conn.putrequest("POST", "/v1/chat/completions")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        conn.send(payload)
        resp = conn.getresponse()
        obj = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(obj["choices"][0]["message"]["content"], "hello")


class TestAuth(ServerCase):
    def setUp(self):
        super().setUp()
        self._patch("check_key", lambda d, s: s == "secret")

    def test_missing_key_401(self):
        status, raw = self._post_chat()
        self.assertEqual(status, 401)
        self.assertIn("Bearer", json.loads(raw)["error"]["message"])

    def test_bearer_header_ok(self):
        status, _ = self._post_chat(headers={"Authorization": "Bearer secret"})
        self.assertEqual(status, 200)

    def test_bearer_case_insensitive_and_quoted(self):
        for value in ("bearer secret", "BEARER secret", 'Bearer "secret"'):
            status, _ = self._post_chat(headers={"Authorization": value})
            self.assertEqual(status, 200, value)

    def test_x_api_key_header_ok(self):
        status, _ = self._post_chat(headers={"x-api-key": "secret"})
        self.assertEqual(status, 200)

    def test_query_key_rejected(self):
        # deliberate design: ?key= must NOT authenticate (leaks via history/Referer)
        status, _ = self._request("/v1/chat/completions?key=***",
                                  body={"model": "hy3",
                                        "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 401)

    def test_keepalive_sync_after_401(self):
        # a rejected POST with an undrained body must not desync the connection
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"model": "hy3", "messages": []})
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        r1 = conn.getresponse()
        r1.read()
        self.assertEqual(r1.status, 401)
        conn.request("GET", "/health")
        r2 = conn.getresponse()
        info = json.loads(r2.read())
        conn.close()
        self.assertEqual(r2.status, 200)
        self.assertTrue(info["ok"])

class TestRealmGuards(ServerCase):
    def test_cn_only_model_auto_routes_without_header(self):
        # regression: glm-5.3-flash on a global-intl gateway used to 400
        # because the global realm masked the per-model auto-routing
        self._patch_upstream("exclusive_realm", lambda m: "cn" if m == "glm-5.3-flash" else "")
        status, _ = self._post_chat(body={"model": "glm-5.3-flash", "messages":
                                          [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(self.realms, [None])   # deferred to model detection

    def test_cn_only_model_ok_with_cn_header(self):
        self._patch_upstream("exclusive_realm", lambda m: "cn")
        status, _ = self._post_chat(headers={"X-Realm": "cn"})
        self.assertEqual(status, 200)
        self.assertEqual(self.realms, ["cn"])

    def test_cross_realm_400_without_upstream(self):
        self._patch_upstream("exclusive_realm", lambda m: "cn")
        status, raw = self._post_chat(headers={"X-Realm": "intl"})
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])

    def test_banned_model_400(self):
        self._patch_upstream("is_model_banned", lambda m: True)
        self._patch_upstream("banned_model_message", lambda m: "banned!")
        status, raw = self._post_chat()
        self.assertEqual(status, 400)
        self.assertIn("banned", json.loads(raw)["error"]["message"])

    def test_rate_limited_maps_429_retry_after(self):
        exc = srv.upstream.RateLimited(None, "slow down", wait=42)
        self.open_result = ("raise", exc)
        status, raw = self._post_chat()
        self.assertEqual(status, 429)
        # urllib exposes Retry-After
        self.assertIn("retry", json.loads(raw)["error"]["message"].lower())

    def test_content_rejected_passthrough_403(self):
        exc = srv.upstream.ContentRejected(RuntimeError("403"), "review blocked")
        self.open_result = ("raise", exc)
        status, raw = self._post_chat()
        self.assertEqual(status, 403)
        self.assertIn("review blocked", json.loads(raw)["error"]["message"])

    def test_no_usable_account_maps_503(self):
        self.open_result = ("raise", RuntimeError("no usable account for realm 'intl'"))
        status, raw = self._post_chat()
        self.assertEqual(status, 503)
        self.assertIn("wbctl login", json.loads(raw)["error"]["message"])

    def test_http_error_maps_status(self):
        req = urllib.request.Request("https://x/y", data=b"")
        exc = urllib.error.HTTPError("https://x/y", 502, "Bad Gateway", {},
                                     _BytesIO(b"upstream says hi"))
        exc.account_uid = "uid-x"
        self.open_result = ("raise", exc)
        status, raw = self._post_chat()
        self.assertEqual(status, 502)
        self.assertIn("upstream says hi", json.loads(raw)["error"]["message"])


class _BytesIO:
    def __init__(self, data):
        import io
        self._b = io.BytesIO(data)

    def read(self, n=-1):
        return self._b.read(n)

    def close(self):
        pass


class TestRealmPrefix(ServerCase):
    def _patch_exclusive(self):
        self._patch_upstream("exclusive_realm",
                             lambda m: "cn" if m == "glm-5.3-flash" else "")

    def test_global_prefix_pins_intl_exit(self):
        # global: on a dual model -> forwarded bare, realm forced to intl
        status, raw = self._post_chat(body={"model": "global:hy3", "messages":
                                            [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[0]["model"], "hy3")   # stripped upstream
        self.assertEqual(self.realms, ["intl"])
        obj = json.loads(raw)
        self.assertEqual(obj["model"], "global:hy3")       # echoed to client

    def test_cn_prefix_pins_cn_exit(self):
        status, _ = self._post_chat(body={"model": "cn:hy3", "messages":
                                          [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[0]["model"], "hy3")
        self.assertEqual(self.realms, ["cn"])

    def test_prefix_beats_header(self):
        self._patch_exclusive()
        # cn: prefix on a cn-exclusive model -> no conflict, pinned cn
        status, _ = self._post_chat(
            body={"model": "cn:glm-5.3-flash", "messages":
                  [{"role": "user", "content": "hi"}]},
            headers={"X-Realm": "cn"})
        self.assertEqual(status, 200)
        self.assertEqual(self.realms, ["cn"])

    def test_prefix_conflict_400(self):
        self._patch_exclusive()
        # global: prefix (intl) onto a cn-only model -> guarded
        status, raw = self._post_chat(body={"model": "global:glm-5.3-flash",
                                            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])
        self.assertIn("前缀", json.loads(raw)["error"]["message"])

    def test_pinned_realm_does_not_leak_across_keepalive(self):
        # regression guard for the per-instance reset: a prefixed request
        # followed by a bare one on the SAME connection must not inherit cn
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for model in ("cn:hy3", "hy3"):
            conn.request("POST", "/v1/chat/completions",
                         body=json.dumps({"model": model,
                                          "messages": [{"role": "user", "content": "x"}]}),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 200, model)
        conn.close()
        self.assertEqual(self.realms, ["cn", None])  # second defers to auto-routing

    def test_stream_echoes_prefixed_model(self):
        body = {"model": "cn:hy3", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
        status, raw = self._post_chat(body)
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        frames = [ln for ln in text.split("\n\n") if ln.startswith("data: ")
                  and ln != "data: [DONE]"]
        for ln in frames:
            obj = json.loads(ln[len("data: "):])
            self.assertEqual(obj.get("model"), "cn:hy3")

    def test_models_lists_union_with_aliases(self):
        # default GET /v1/models now carries both exits + cn:/global: aliases
        status, raw = self._request("/v1/models")
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(data["realm"], "all")
        ids = [m["id"] for m in data["data"]]
        self.assertEqual(ids, ["hy3", "cn:hy3", "global:hy3"])

    def test_models_with_header_stays_narrow(self):
        status, raw = self._request("/v1/models", headers={"X-Realm": "cn"})
        data = json.loads(raw)
        self.assertEqual(data["realm"], "cn")
        self.assertEqual([m["id"] for m in data["data"]], ["hy3"])

    def test_responses_prefix_routes_and_echoes(self):
        self._patch_exclusive()
        status, raw = self._request("/v1/responses",
                                    body={"model": "global:glm-5.3-flash", "input": []})
        self.assertEqual(status, 400)   # explicit intl pin vs cn-exclusive model
        status, raw = self._request("/v1/responses",
                                    body={"model": "cn:glm-5.3-flash", "input": []})
        self.assertEqual(status, 200)
        obj = json.loads(raw)
        self.assertEqual(obj.get("model"), "cn:glm-5.3-flash")
        self.assertEqual(self.realms[-1], "cn")


class TestResponsesRoute(ServerCase):
    def test_nonstream_translation(self):
        body = {"model": "hy3",
                "input": [{"role": "user", "content": "hi"}]}
        status, raw = self._request("/v1/responses", body=body)
        obj = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(obj.get("object"), "response")
        self.assertEqual(obj["output"][0]["content"][0]["text"], "hello")

    def test_previous_response_id_rejected(self):
        status, raw = self._request("/v1/responses",
                                    body={"model": "hy3", "previous_response_id": "r1",
                                          "input": []})
        self.assertEqual(status, 400)
        self.assertIn("previous_response_id", json.loads(raw)["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
