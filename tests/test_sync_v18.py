"""Tests for token recovery, streaming deltas, desktop headers and session ledgers."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))

from gateway import identity  # noqa: E402
from gateway import translate as tr  # noqa: E402
from gateway.accounts import Account  # noqa: E402
from gateway.tasks.scheduler import Scheduler  # noqa: E402
from helpers_jwt import make_jwt  # noqa: E402


def _frames(chunks):
    out = [("data: " + json.dumps(c) + "\n\n").encode() for c in chunks]
    out.append(b"data: [DONE]\n\n")
    return out


def _events(chunks, holder=None):
    raw = b"".join(tr.stream_responses_events(
        iter(_frames(chunks)), "m", holder or {"usage": None})).decode("utf-8")
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]


class TokenRecovery(unittest.TestCase):
    # a served request lifts a credential-shaped park.
    def test_success_clears_token_expired(self):
        a = Account({"accessToken": make_jwt(uid="u1"), "uid": "u1"})
        a.note_error("HTTP 401: unauthorized")
        self.assertTrue(a.token_expired)
        self.assertFalse(a.ready(allow_refresh=False))
        a.note_success()
        self.assertFalse(a.token_expired)
        self.assertTrue(a.ready(allow_refresh=False))

    def test_html_proxy_401_never_parks(self):
        a = Account({"accessToken": make_jwt(uid="u1"), "uid": "u1"})
        a.note_error("<html><body>401 Authorization Required</body></html>")
        self.assertFalse(a.token_expired)


class FirstSegmentDeltas(unittest.TestCase):
    # the first tool-call chunk may already carry arguments.
    def _single(self, first_args, later_args):
        chunks = [{"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_first", "type": "function",
            "function": {"name": "get_weather", "arguments": first_args}}]}}]}]
        chunks.extend({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": part}}]}}]} for part in later_args)
        chunks.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        return _events(chunks)

    def test_first_fragment_reaches_the_stream_once(self):
        for first_args, later_args in [
                ('{"city":"', ['Beijing"}']),
                ('{"city":"Beijing"}', []),
                ("", ['{"city":"Beijing"}']),
        ]:
            with self.subTest(first=first_args):
                events = self._single(first_args, later_args)
                deltas = [e for e in events
                          if e["type"] == "response.function_call_arguments.delta"]
                done = [e for e in events
                        if e["type"] == "response.function_call_arguments.done"]
                self.assertEqual("".join(e["delta"] for e in deltas),
                                 '{"city":"Beijing"}')
                self.assertEqual(len(done), 1)
                self.assertEqual(done[0]["arguments"],
                                 "".join(e["delta"] for e in deltas))
                self.assertTrue(all(e["delta"] for e in deltas))
                added = next(i for i, e in enumerate(events)
                             if e["type"] == "response.output_item.added")
                self.assertTrue(all(i > added for i, e in enumerate(events)
                                    if e["type"] == "response.function_call_arguments.delta"))

    def test_interleaved_tools_keep_their_own_segments(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "function":
                 {"name": "get_weather", "arguments": '{"city":"'}},
                {"index": 1, "id": "call_b", "function":
                 {"name": "get_weather", "arguments": '{"city":"Paris"}'}},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'Beijing"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = _events(chunks)
        for index, expected in [(0, '{"city":"Beijing"}'), (1, '{"city":"Paris"}')]:
            actual = "".join(e["delta"] for e in events
                             if e["type"] == "response.function_call_arguments.delta"
                             and e["output_index"] == index)
            self.assertEqual(actual, expected)

    def test_custom_tools_use_the_custom_delta_path(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_custom_first", "function":
                 {"name": "apply_patch", "arguments": '{"input":"'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'pwd"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = _events(chunks, {"usage": None, "custom_names": {"apply_patch"}})
        custom = "".join(e["delta"] for e in events
                         if e["type"] == "response.custom_tool_call_input.delta")
        self.assertEqual(custom, '{"input":"pwd"}')
        self.assertFalse([e for e in events
                          if e["type"].startswith("response.function_call_arguments.")])


class DesktopChatCompat(unittest.TestCase):
    # WB_DESKTOP_CHAT_COMPAT=1 opts desktop chat into the SDK profile.
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("WB_DESKTOP_CHAT_COMPAT", None)

    def account(self, product="workbuddy", realm="intl"):
        return Account({"uid": "desktop-fixture", "accessToken": "fixture",
                        "product": product, "realm": realm})

    def test_default_keeps_the_browser_profile(self):
        headers = self.account().headers()
        self.assertIn("Origin", headers)
        self.assertNotIn("x-stainless-runtime", headers)
        self.assertNotIn("X-B3-ParentSpanId", headers)

    def test_only_explicit_one_enables_it(self):
        for value in ("0", "false", "true", "yes", ""):
            with self.subTest(value=value):
                os.environ["WB_DESKTOP_CHAT_COMPAT"] = value
                self.assertFalse(identity.desktop_chat_compat_enabled())
        os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
        self.assertTrue(identity.desktop_chat_compat_enabled())

    def test_compat_switches_desktop_chat_to_sdk_headers(self):
        os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
        for realm in ("cn", "intl"):
            with self.subTest(realm=realm):
                headers = self.account(realm=realm).headers()
                self.assertEqual(headers["Accept"], "application/json")
                for name in ("Origin", "Referer", "Accept-Language",
                             "X-Requested-With", "X-CodeBuddy-Request"):
                    self.assertNotIn(name, headers)
                self.assertEqual(headers["x-stainless-runtime"], "node")
                self.assertNotIn("x-stainless-timeout", headers)

    def test_b3_parent_is_consistent(self):
        os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
        headers = self.account().headers()
        trace, span, sampled, parent = headers["b3"].split("-")
        self.assertEqual(headers["X-B3-TraceId"], trace)
        self.assertEqual(headers["X-B3-SpanId"], span)
        self.assertEqual(headers["X-B3-ParentSpanId"], parent)
        self.assertEqual((len(trace), len(span), len(parent), sampled),
                         (32, 16, 16, "1"))
        self.assertEqual(headers["traceparent"], "00-%s-%s-01" % (trace, span))

    def test_cli_and_billing_are_untouched(self):
        stable = ("Accept", "Origin", "Referer", "Accept-Language",
                  "X-Requested-With", "User-Agent", "x-stainless-runtime",
                  "x-stainless-timeout", "X-IDE-Type")
        for product, purpose in (("cli", "chat"), ("workbuddy", "billing")):
            with self.subTest(product=product, purpose=purpose):
                os.environ.pop("WB_DESKTOP_CHAT_COMPAT", None)
                before = self.account(product).headers(purpose=purpose)
                os.environ["WB_DESKTOP_CHAT_COMPAT"] = "1"
                after = self.account(product).headers(purpose=purpose)
                self.assertEqual({k: before.get(k) for k in stable},
                                 {k: after.get(k) for k in stable})


class ModelWindowPersistence(unittest.TestCase):
    # 429 reset instants live in the credential file.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _account(self, data=None, path=None):
        data = data or {"accessToken": make_jwt(uid="u1"), "uid": "u1",
                        "realm": "intl"}
        return Account(data, path)

    def _saved(self):
        a = self._account()
        a.save(self.tmp.name)
        return a

    def _reload(self):
        path = os.path.join(self.tmp.name, "u1.json")
        with open(path, encoding="utf-8") as fh:
            return Account(json.load(fh), path)

    def test_reset_instant_is_persisted_and_rearmed(self):
        a = self._saved()
        until = time.time() + 600
        a.note_error("HTTP 429: rate limit", model="hy3", until=until)
        restored = self._reload()
        self.assertGreater(restored.throttle_wait("hy3"), 500)
        self.assertFalse(restored.ready(model="hy3", allow_refresh=False))
        self.assertTrue(restored.ready(model="other", allow_refresh=False))

    def test_window_lapses_on_its_own(self):
        a = self._saved()
        a.note_error("HTTP 429", model="hy3", until=time.time() + 3600)
        self.assertGreater(a.throttle_wait("hy3"), 0)
        # No sweeper exists; the clock comparison is the expiry.
        a.model_cooldowns["hy3"] = time.time() - 1
        self.assertEqual(a.throttle_wait("hy3"), 0)
        self.assertTrue(a.ready(model="hy3", allow_refresh=False))

    def test_expired_windows_are_not_restored(self):
        data = {"accessToken": make_jwt(uid="u1"), "uid": "u1", "realm": "intl",
                "modelWindows": {"hy3": time.time() - 60}}
        a = self._account(data)
        self.assertEqual(a.model_cooldowns, {})
        self.assertEqual(a.throttle_wait("hy3"), 0)

    def test_success_drops_the_persisted_window(self):
        a = self._saved()
        a.note_error("HTTP 429", model="hy3", until=time.time() + 600)
        self.assertIn("hy3", self._reload().model_cooldowns)
        a.note_success(model="hy3")
        self.assertNotIn("hy3", self._reload().model_cooldowns)

    def test_pathless_account_never_writes(self):
        a = self._account()
        a.note_error("HTTP 429", model="hy3", until=time.time() + 60)
        self.assertIn("hy3", a.model_cooldowns)
        self.assertIsNone(a.path)


class WebSessionLedger(unittest.TestCase):
    # ledger is bounded and retries failed releases.
    def _account(self):
        return Account({"accessToken": make_jwt(uid="u1"), "uid": "u1",
                        "realm": "intl"})

    def test_register_and_release(self):
        a = self._account()
        a.web_session_register("conv-1")
        self.assertEqual(list(a._web_sessions), ["conv-1"])
        self.assertTrue(a.web_session_release("conv-1"))
        self.assertFalse(a.web_session_release("conv-1"))
        self.assertEqual(a._web_sessions, {})

    def test_reclaim_closes_only_stale_sessions(self):
        a = self._account()
        a.web_session_register("conv-1")
        calls = []
        with mock.patch.object(a, "web_conversation_release",
                               side_effect=lambda conv, delete=False:
                               calls.append((conv, delete)) or True):
            self.assertEqual(a.reclaim_web_sessions(max_age=10 ** 6), 0)
            self.assertEqual(calls, [])
            self.assertEqual(a.reclaim_web_sessions(max_age=0), 1)
        self.assertEqual(calls, [("conv-1", True)])
        self.assertEqual(a._web_sessions, {})

    def test_failed_reclaim_keeps_the_entry_for_retry(self):
        a = self._account()
        a.web_session_register("conv-1")
        with mock.patch.object(a, "web_conversation_release", return_value=False):
            self.assertEqual(a.reclaim_web_sessions(max_age=0), 0)
        self.assertIn("conv-1", a._web_sessions)
        with mock.patch.object(a, "web_conversation_release", return_value=True):
            self.assertEqual(a.reclaim_web_sessions(max_age=0), 1)
        self.assertEqual(a._web_sessions, {})

    def test_registry_is_bounded_and_drops_the_oldest(self):
        a = self._account()
        for index in range(130):
            a.web_session_register("conv-%03d" % index)
        self.assertEqual(len(a._web_sessions), 128)
        self.assertNotIn("conv-000", a._web_sessions)
        self.assertIn("conv-129", a._web_sessions)

    def test_every_created_session_is_released(self):
        a = self._account()
        a._web_conversation_create = lambda body: ("conv-1", None)

        def boom(conversation, suffix=""):
            raise RuntimeError("socket wedged")

        a._web_conversation_get = boom
        calls = []
        with mock.patch.object(a, "web_conversation_release",
                               side_effect=lambda conv, delete=False:
                               calls.append((conv, delete)) or True):
            res = a.daily_chat_web()
        self.assertFalse(res["ok"])
        self.assertEqual(calls, [("conv-1", True)])
        self.assertEqual(a._web_sessions, {})

    def test_reclaim_honours_the_delete_intent(self):
        a = self._account()
        a.web_session_register("conv")
        a.web_session_mark("conv", delete=False)  # finished run: keep history
        calls = []
        with mock.patch.object(a, "web_conversation_release",
                               side_effect=lambda conv, delete=False:
                               calls.append(delete) or True):
            self.assertEqual(a.reclaim_web_sessions(max_age=0), 1)
        self.assertEqual(calls, [False])

    def test_scheduler_sweeps_every_account(self):
        class Pool(object):
            dir = None
            accounts = [self._account(), self._account()]

        for acc in Pool.accounts:
            acc.web_session_register("conv")
            acc._web_sessions["conv"]["ts"] = time.time() - 10 ** 6  # abandoned
        sched = Scheduler(Pool(), enabled=False)
        with mock.patch.object(Account, "web_conversation_release", return_value=True):
            self.assertEqual(sched.reclaim_web_sessions(), 2)
        for acc in Pool.accounts:
            self.assertEqual(acc._web_sessions, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
