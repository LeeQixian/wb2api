"""Tests for the v1.9.1 -> v1.10.9 sync: tool names, namespaces, Codex intro,
429 reset instant, error-body cache, SSE terminal stop, usage append, fold seam."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from gateway.http import state as http_state  # noqa: E402
from gateway.http.base import HTTPBase, _usable_reset_epoch  # noqa: E402
from gateway.http.usage import record_row  # noqa: E402
from gateway.protocol.request import normalize_tools  # noqa: E402
from gateway.protocol.responses_in import responses_to_chat  # noqa: E402
from gateway.protocol.sanitize import has_fingerprint, sanitize_text  # noqa: E402
from gateway.protocol.stream import aggregate_stream  # noqa: E402
from gateway.protocol.tools import expand_namespace_tools, resolve_namespaced_name  # noqa: E402
from gateway.upstream import guards, retry  # noqa: E402
from gateway.upstream.errors import RateLimited  # noqa: E402


def chat_body(tools):
    return {"model": "hy3", "messages": [{"role": "user", "content": "hi"}],
            "tools": tools}


class ToolNameValidation(unittest.TestCase):
    """b04868e: names the upstream validator rejects are dropped, not renamed."""

    def normalized(self, tools):
        body = chat_body(tools)
        normalize_tools(body)
        return body

    def test_server_side_declaration_is_dropped(self):
        # {"type": "web_search"} used to normalize to {"type": "function"}
        # with no function object, failing the whole request at
        # tools[N].function.name (code 11133).
        body = self.normalized([
            {"type": "function", "function": {"name": "ok", "parameters": {}}},
            {"type": "web_search"},
        ])
        self.assertEqual([t["function"]["name"] for t in body["tools"]], ["ok"])

    def test_unusable_names_are_dropped(self):
        body = self.normalized([
            {"type": "function", "function": {"name": "bad.name"}},
            {"type": "function", "function": {"name": "bad/name"}},
            {"type": "function", "function": {"name": ""}},
            {"type": "function", "function": {"name": "x" * 65}},
            {"type": "function", "function": {"name": "good-name_1"}},
        ])
        self.assertEqual([t["function"]["name"] for t in body["tools"]],
                         ["good-name_1"])

    def test_duplicates_keep_the_first(self):
        body = self.normalized([
            {"type": "function", "function": {"name": "dup", "description": "a"}},
            {"type": "function", "function": {"name": "dup", "description": "b"}},
        ])
        self.assertEqual(len(body["tools"]), 1)
        self.assertEqual(body["tools"][0]["function"]["description"], "a")

    def test_all_dropped_removes_the_key(self):
        body = self.normalized([{"type": "web_fetch"}])
        self.assertNotIn("tools", body)


class NamespaceTools(unittest.TestCase):
    """ef479f7: nested namespaces keep the parent path; tool_choice unwraps."""

    def test_nested_namespace_keeps_parent_path(self):
        flat, mapping = expand_namespace_tools([{
            "type": "namespace", "name": "outer",
            "tools": [{"type": "namespace", "name": "inner",
                       "tools": [{"type": "function", "name": "run"}]}],
        }])
        self.assertEqual([t["name"] for t in flat], ["run"])
        self.assertEqual(mapping["run"], "outer__inner")

    def test_deep_returned_name_splits_on_the_last_separator(self):
        self.assertEqual(resolve_namespaced_name("outer::inner::run",
                                                 {"run": "outer__inner"}),
                         ("run", "outer__inner"))

    def test_tool_choice_is_written_back_as_the_bare_name(self):
        chat = responses_to_chat({
            "model": "hy3", "input": "hi",
            "tools": [{"type": "namespace", "name": "outer", "tools": [
                {"type": "namespace", "name": "inner", "tools": [
                    {"type": "function", "name": "run"}]}]}],
            "tool_choice": {"type": "function",
                            "function": {"name": "outer::inner::run"}},
        })
        self.assertEqual(chat["tool_choice"]["function"]["name"], "run")


class CodexIntroNeutralized(unittest.TestCase):
    """ecb95a6: the old rewrite injected the very fingerprint it targeted."""

    INTRO = ("You are a coding agent running in the Codex CLI, a terminal-based "
             "coding assistant.")

    def test_intro_sentence_is_replaced_and_no_longer_a_fingerprint(self):
        out = sanitize_text(self.INTRO)
        self.assertNotIn("Codex", out)
        self.assertFalse(has_fingerprint(out))

    def test_shortened_intro_is_also_neutralized(self):
        out = sanitize_text("You are a coding agent running in the Codex CLI.")
        self.assertNotIn("Codex", out)

    def test_neutral_text_is_untouched(self):
        text = "Please summarize this repository."
        self.assertEqual(sanitize_text(text), text)

    def test_default_branch_rewrite_is_now_idempotent(self):
        text = ("Main branch (you will usually use this for PRs) is protected.")
        self.assertEqual(sanitize_text(text), text)


class _Recorder(object):
    """Just enough of a handler for HTTPBase._rate_limited."""

    def __init__(self):
        self.status = None
        self.headers = []
        self.wfile = io.BytesIO()

    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _cors(self):
        pass

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass

    def header(self, key):
        for k, v in self.headers:
            if k == key:
                return v
        return None


class RateLimitResetInstant(unittest.TestCase):
    """0ed5386: every 429 exit carries the absolute instant the window reopens."""

    def emit(self, exc):
        rec = _Recorder()
        HTTPBase._rate_limited(rec, exc)
        return rec, json.loads(rec.wfile.getvalue().decode("utf-8"))["error"]

    def test_explicit_reset_at_wins_over_the_local_window(self):
        target = int(time.time()) + 900
        rec, err = self.emit(RateLimited(None, "slow down", wait=42, reset_at=target))
        self.assertEqual(rec.status, 429)
        self.assertEqual(rec.header("X-RateLimit-Reset"), str(target))
        self.assertEqual(err["reset_at"], target)
        self.assertEqual(rec.header("Retry-After"), "42")

    def test_missing_reset_at_falls_back_to_the_same_window(self):
        now = int(time.time())
        rec, err = self.emit(RateLimited(None, "slow down", wait=600))
        self.assertGreaterEqual(err["reset_at"], now + 599)
        self.assertLessEqual(err["reset_at"], now + 602)
        self.assertEqual(rec.header("X-RateLimit-Reset"), str(err["reset_at"]))
        self.assertTrue(rec.header("X-RateLimit-Reset-At").endswith("Z"))

    def test_stale_or_bogus_instants_fall_back(self):
        now = int(time.time())
        for bad in (now - 100, 0, "nope", 9999999999):
            rec, err = self.emit(RateLimited(None, "slow down", wait=60, reset_at=bad))
            self.assertGreaterEqual(err["reset_at"], now + 59, bad)

    def test_filter_rejects_past_zero_and_far_future(self):
        now = int(time.time())
        self.assertIsNone(_usable_reset_epoch(now - 1))
        self.assertIsNone(_usable_reset_epoch(0))
        self.assertIsNone(_usable_reset_epoch(4102444801))
        self.assertIsNone(_usable_reset_epoch("abc"))


class ErrorBodyCache(unittest.TestCase):
    """97df705: opt-in reuse of the first error-body read."""

    def make_exc(self):
        return urllib.error.HTTPError("https://x/y", 429, "Limited", {},
                                      io.BytesIO(b'{"code":6004}'))

    def test_enabled_reads_are_stable(self):
        os.environ["WB_CACHE_HTTP_ERROR_BODY"] = "1"
        try:
            exc = self.make_exc()
            self.assertEqual(retry.read_http_error(exc), b'{"code":6004}')
            self.assertEqual(retry.read_http_error(exc), b'{"code":6004}')
        finally:
            os.environ.pop("WB_CACHE_HTTP_ERROR_BODY", None)

    def test_disabled_keeps_the_old_single_read(self):
        exc = self.make_exc()
        self.assertEqual(retry.read_http_error(exc), b'{"code":6004}')
        self.assertEqual(retry.read_http_error(exc), b"")


class SseTerminalStop(unittest.TestCase):
    """1bf70e5: opt-in stop at [DONE]; default preserves the old behavior."""

    LINES = [
        b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n',
        b'data: [DONE]\n',
        b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"late"}}]}\n',
        b'data: [DONE]\n',
    ]

    def run_aggregate(self):
        return aggregate_stream(iter(self.LINES), "hy3", "resp")

    def test_enabled_stops_at_the_first_terminal_marker(self):
        os.environ["WB_STOP_AT_SSE_DONE"] = "1"
        try:
            out = self.run_aggregate()
        finally:
            os.environ.pop("WB_STOP_AT_SSE_DONE", None)
        self.assertEqual(out["choices"][0]["message"]["content"], "hi")

    def test_disabled_reads_the_rest_of_the_stream(self):
        out = self.run_aggregate()
        self.assertEqual(out["choices"][0]["message"]["content"], "hilate")


sys.path.insert(0, os.path.join(_ROOT, "tests"))
from test_server import FakeAccount, FakeResponse, ServerCase  # noqa: E402


class SsePassthroughStop(ServerCase):
    """The forwarded (stream=true) path must honor the same switch."""

    LINES = SseTerminalStop.LINES

    def stream_chat(self):
        self.open_result = ("ok", None)
        return self._post_chat(body={
            "model": "hy3", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})

    def test_enabled_does_not_forward_frames_after_done(self):
        self._patch_upstream("open_upstream", lambda *a, **k: (FakeResponse(self.LINES), FakeAccount()))
        os.environ["WB_STOP_AT_SSE_DONE"] = "1"
        try:
            status, raw = self.stream_chat()
        finally:
            os.environ.pop("WB_STOP_AT_SSE_DONE", None)
        self.assertEqual(status, 200)
        self.assertIn(b'"hi"', raw)
        self.assertNotIn(b"late", raw)

    def test_disabled_forwards_everything(self):
        self._patch_upstream("open_upstream", lambda *a, **k: (FakeResponse(self.LINES), FakeAccount()))
        status, raw = self.stream_chat()
        self.assertEqual(status, 200)
        self.assertIn(b"late", raw)


class UsageAtomicAppend(unittest.TestCase):
    """53c8697: concurrent writers neither interleave nor lose rows."""

    def test_threads_append_one_parseable_line_each(self):
        saved_usage, saved_pool = http_state.USAGE_DIR, http_state.POOL
        with tempfile.TemporaryDirectory() as tmp:
            http_state.USAGE_DIR = tmp
            http_state.POOL = None
            try:
                writers, per_writer = 8, 40
                errors = []

                def work(n):
                    try:
                        for i in range(per_writer):
                            record_row({"model": "hy3", "outcome": "completed",
                                        "account": "u%d" % n, "i": i})
                    except Exception as exc:      # noqa: BLE001
                        errors.append(exc)

                threads = [threading.Thread(target=work, args=(n,))
                           for n in range(writers)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [])
                with open(os.path.join(tmp, "usage.jsonl"), encoding="utf-8") as fh:
                    rows = [json.loads(line) for line in fh if line.strip()]
                self.assertEqual(len(rows), writers * per_writer)
            finally:
                http_state.USAGE_DIR, http_state.POOL = saved_usage, saved_pool


class DailyFoldSeam(unittest.TestCase):
    """b700692: an in-place rewrite must restart the fold, not silently skip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_dir = guards._state.USAGE_DIR
        guards._state.USAGE_DIR = self.tmp.name
        guards._daily_usage.update({"day": "", "totals": None, "offset": 0,
                                    "at": 0.0, "key": None, "tail": None})
        self.path = os.path.join(self.tmp.name, "usage.jsonl")

    def tearDown(self):
        guards._state.USAGE_DIR = self.saved_dir
        guards._daily_usage.update({"day": "", "totals": None, "offset": 0,
                                    "at": 0.0, "key": None, "tail": None})
        self.tmp.cleanup()

    def write(self, rows, mode="w"):
        with open(self.path, mode, encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    @staticmethod
    def row(uid, tokens):
        return {"at": time.time(), "account": uid, "total_tokens": tokens,
                "outcome": "completed"}

    def fold(self):
        return guards.daily_tokens_by_account(ttl=0)

    def test_plain_append_keeps_the_incremental_fold(self):
        self.write([self.row("u1", 10)])
        self.assertEqual(self.fold(), {"u1": 10})
        self.write([self.row("u1", 5)], mode="a")
        self.assertEqual(self.fold(), {"u1": 15})

    def test_copytruncate_and_regrow_past_the_old_offset(self):
        # Long first generation so the rewritten file can grow past its offset.
        self.write([self.row("u1", 10)] * 40)
        self.assertEqual(self.fold(), {"u1": 400})
        self.assertIsNotNone(guards._daily_usage["key"])
        self.write([self.row("u2", 7)] * 80)      # same inode, emptied in place
        self.assertEqual(self.fold(), {"u2": 560})

    def test_same_size_rewrite_is_caught(self):
        rows = [self.row("u1", 10)] * 20
        self.write(rows)
        self.assertEqual(self.fold(), {"u1": 200})
        self.write([self.row("u2", 10)] * 20)     # identical length, new data
        self.assertEqual(self.fold(), {"u2": 200})

    def test_missing_log_returns_none(self):
        self.assertIsNone(self.fold())


if __name__ == "__main__":
    unittest.main()