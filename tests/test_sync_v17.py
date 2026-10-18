"""Regression pins for the hub v1.6.18..v1.7.2 sync batch.
Each test defends one ported upstream fix; hub commits cited per class."""
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))

from gateway import settings as st  # noqa: E402
from gateway import translate as tr  # noqa: E402
from gateway import upstream as up  # noqa: E402
from gateway import accounts as acc_mod  # noqa: E402
from gateway.accounts import Account  # noqa: E402
from helpers_jwt import make_jwt  # noqa: E402
from test_upstream import (FakeAccount, FakePool, http_error,  # noqa: E402
                           fake_urlopen_from)


def fake_http_error(code, payload):
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


class ResetWording(unittest.TestCase):
    # hub a351df9: the cn 429 wording ("... will reset at ...")
    def test_cn_and_en_both_parse(self):
        cn = up.parse_rate_limit_reset("用量已超出频率限制，将在 2026-10-09 14:44:59 UTC+8 重置")
        en = up.parse_rate_limit_reset("usage will reset at 2026-10-09 14:44:59 UTC+8")
        self.assertEqual(cn, en)
        self.assertIsNotNone(cn)

    def test_absent_time_returns_none(self):
        self.assertIsNone(up.parse_rate_limit_reset("usage exceeds frequency limit"))


class ScopePhrases(unittest.TestCase):
    # hub 72e51b4: word-boundary matching; identifiers are not scope statements
    def test_metadata_tail_does_not_scoped_match(self):
        body = '{"code":14003,"msg":"too many requests","accountId":"a","credentialId":"c"}'
        self.assertFalse(up.rate_limit_is_account_level(body, None))
        self.assertTrue(up.rate_limit_is_soft_shape(body))

    def test_positive_evidence_in_both_languages(self):
        self.assertTrue(up.rate_limit_is_account_level("this account is rate limited", None))
        self.assertTrue(up.rate_limit_is_account_level("该账号触发限流", None))
        self.assertFalse(up.rate_limit_is_account_level("account level", 123.0))

    def test_legacy_local_markers_still_count(self):
        self.assertTrue(up.rate_limit_is_account_level(
            "hourly cap reached", None))


class UnscopedLadder(unittest.TestCase):
    # hub 72e51b4: separate 60s->600s ladder; never feeds the credential ladder
    def setUp(self):
        self.a = Account({"accessToken": make_jwt(), "uid": "u1", "realm": "cn"})

    def test_short_ladder_caps_at_600(self):
        self.assertEqual(self.a.note_unscoped_rate("hy3"), 60.0)
        self.assertEqual(self.a.note_unscoped_rate("hy3"), 120.0)
        for _ in range(10):
            self.a.note_unscoped_rate("hy3")
        self.assertEqual(self.a.note_unscoped_rate("hy3"), 600.0)

    def test_counters_do_not_cross_feed(self):
        self.a.note_unscoped_rate("hy3")
        self.a.note_unscoped_rate("hy3")
        wait = self.a.note_soft_rate("HTTP 429 (account soft rate)")
        self.assertEqual(wait, 600.0)  # credential ladder starts fresh
        self.a.note_success()
        self.assertEqual(self.a.soft_streak, 0)
        self.assertEqual(self.a.unscoped_streak, 0)


class Dispatch429(unittest.TestCase):
    # hub dc1a565/72e51b4 at the dispatch level
    def open_with(self, pool, responses, **kw):
        payload = {"model": "hy3", "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(up.wb_accounts, "urlopen",
                               fake_urlopen_from(responses)), \
             mock.patch.object(time, "sleep"):
            return up.open_upstream(pool, payload, **kw)

    def test_unscoped_shape_uses_short_ladder(self):
        a = FakeAccount("u1")
        b = FakeAccount("u2")
        resp, acct = self.open_with(FakePool([a, b]),
                                    [http_error(429, b"usage exceeds frequency limit"),
                                     b"ok"])
        self.assertEqual(acct.uid, "u2")
        self.assertEqual(a.unscoped_streak, 1)
        self.assertTrue(any(e["msg"] == "unscoped 429" for e in a.errors))
        self.assertEqual(a.soft_streak, 0)

    def test_unrecognized_body_flat_60_no_streak(self):
        a = FakeAccount("u1")
        b = FakeAccount("u2")
        self.open_with(FakePool([a, b]), [http_error(429, b"some html page"), b"ok"])
        self.assertEqual(getattr(a, "unscoped_streak", 0), 0)
        self.assertTrue(any("model throttled" in (e["msg"] or "") for e in a.errors))


class Restricted403(unittest.TestCase):
    # hub be4f9f8
    def test_recognition_and_false_positive(self):
        hit = '{"code":11140,"msg":"request illegal","displayMsg":{"en":"Your ' \
              'account has been restricted due to unusual activity"}}'
        self.assertTrue(up.restriction_is_account_level(hit))
        self.assertTrue(up.restriction_is_account_level('{"code":"11140"}'))
        self.assertTrue(up.restriction_is_account_level("账号存在异常行为，已被限制使用"))
        self.assertFalse(up.restriction_is_account_level(
            '{"code":10000,"requestId":"req11140abc"}'))
        self.assertFalse(up.restriction_is_account_level("content policy violation"))

    def open_with(self, pool, responses):
        payload = {"model": "hy3", "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(up.wb_accounts, "urlopen",
                               fake_urlopen_from(responses)), \
             mock.patch.object(time, "sleep"):
            return up.open_upstream(pool, payload)

    def test_restricted_account_parked_then_rotates(self):
        a = FakeAccount("u1")
        b = FakeAccount("u2")
        body = json.dumps({"code": 11140,
                           "msg": "restricted due to unusual activity"}).encode()
        resp, acct = self.open_with(FakePool([a, b]), [http_error(403, body), b"ok"])
        self.assertEqual(acct.uid, "u2")
        self.assertGreater(a.cooldown_until, time.time() + 7000)

    def test_content_review_403_still_passes_through(self):
        # local contract: content review keeps the passthrough path (no DegradeGate)
        a = FakeAccount("u1")
        with self.assertRaises(up.ContentRejected):
            self.open_with(FakePool([a]), [http_error(403, b"review blocked")])
        self.assertEqual(a.errors, [])

    def test_unknown_5xx_rotates(self):
        a = FakeAccount("u1")
        b = FakeAccount("u2")
        resp, acct = self.open_with(FakePool([a, b]), [http_error(550), b"ok"])
        self.assertEqual(acct.uid, "u2")
        self.assertEqual(a.fails, 1)


class Checkin10001(unittest.TestCase):
    # hub 0ba3a29: upstream answers "already checked in" as HTTP 400/code 10001
    def test_already_checked_in_counts_success(self):
        d = tempfile.mkdtemp()
        a = Account({"accessToken": make_jwt(), "uid": "c1", "realm": "cn"})
        a.path = os.path.join(d, "c1.json")
        a.save(d)
        with mock.patch.object(acc_mod, "http_json",
                               side_effect=fake_http_error(400, {
                                   "code": 10001, "msg": "今天已签到，请明天再来"})):
            res = a.checkin()
        self.assertTrue(res["ok"])
        self.assertTrue(a.last_checkin)
        self.assertTrue(os.path.exists(a.path))

    def test_other_400_stays_failure(self):
        a = Account({"accessToken": make_jwt(), "uid": "c2", "realm": "cn"})
        with mock.patch.object(acc_mod, "http_json",
                               side_effect=fake_http_error(400, {"code": 11101,
                                                                 "msg": "bad params"})):
            res = a.checkin()
        self.assertFalse(res["ok"])


class StopNormalize(unittest.TestCase):
    # hub c3f263d: upstream Go struct pins stop to []string (code 11101)
    def body(self, stop):
        p = {"model": "hy3", "messages": [{"role": "user", "content": "x"}]}
        if stop is not None:
            p["stop"] = stop
        return tr.build_upstream_body(p)

    def test_string_wraps_to_list(self):
        self.assertEqual(self.body("END")["stop"], ["END"])

    def test_list_filtered_order_kept(self):
        self.assertEqual(self.body(["a", "", 5, None, "b"])["stop"], ["a", "b"])

    def test_empty_forms_drop_field(self):
        for v in ("", [], None, {}, 7):
            self.assertNotIn("stop", self.body(v), repr(v))
        self.assertNotIn("stop", self.body(object()))


class AdditionalTools(unittest.TestCase):
    # hub fb10152 + 23964ac (Codex Desktop 0.162+)
    CUSTOM_EXEC = {"type": "custom", "name": "exec",
                   "format": {"type": "grammar", "syntax": "lark", "definition": "s: /x/"}}
    PLAIN = {"type": "function", "name": "wait",
             "parameters": {"type": "object", "properties": {}}}

    def ns(self, field="tools"):
        return {"type": "namespace", "name": "functions", field: [self.CUSTOM_EXEC,
                                                                  self.PLAIN]}

    def test_nested_custom_collected(self):
        self.assertEqual(tr.custom_tool_names([self.ns()]), {"exec"})
        self.assertNotIn("wait", tr.custom_tool_names([self.ns()]))
        for field in ("tools", "children", "functions"):
            self.assertEqual(tr.custom_tool_names([self.ns(field)]), {"exec"})

    def test_input_items_merged_outbound(self):
        payload = {"model": "m", "input": [
            {"type": "additional_tools",
             "tools": [{"type": "function", "name": "exec_command"}]},
            {"role": "user", "content": "run ls"}]}
        chat = tr.responses_to_chat(payload)
        self.assertTrue(any(t.get("name") == "exec_command" for t in chat["tools"]))
        self.assertEqual(len(chat["messages"]), 1)  # not leaked into messages

    def test_round_trip_kinds(self):
        names = tr.custom_tool_names([self.ns()])
        nsmap = tr.responses_to_chat(
            {"model": "m", "input": "x", "tools": [self.ns()]})["_namespace_map"]
        exec_msg = {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "exec", "arguments": json.dumps({"input": "pwd"})}}]}}]}
        item = tr.chat_to_response(exec_msg, "m", names,
                                   namespace_map=nsmap)["output"][0]
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item.get("namespace"), "functions")
        self.assertEqual(item.get("input"), "pwd")
        wait_msg = {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "type": "function", "function": {
                    "name": "wait", "arguments": '{"ms":100}'}}]}}]}
        item2 = tr.chat_to_response(wait_msg, "m", names,
                                    namespace_map=nsmap)["output"][0]
        self.assertEqual(item2["type"], "function_call")
        self.assertEqual(item2.get("arguments"), '{"ms":100}')

    def test_finish_reason_empty_string_normalized(self):
        # hub #296
        chunk = '{"choices":[{"delta":{"content":"x"},"finish_reason":""}]}'
        cleaned = json.loads(tr.clean_chunk(chunk))
        self.assertIsNone(cleaned["choices"][0]["finish_reason"])


class UnavailableReason(unittest.TestCase):
    # hub f398a3c
    def setUp(self):
        self.a = Account({"accessToken": make_jwt(exp=time.time() + 7200),
                          "uid": "u1", "realm": "cn"})

    def test_ready_account_says_nothing(self):
        self.assertEqual(self.a.unavailable_reason(), "")

    def test_penalties_and_limits(self):
        self.a.enabled = False
        self.assertEqual(self.a.unavailable_reason(), "disabled")
        self.a.enabled = True
        self.a.breaker_until = time.time() + 1800
        self.a.cooldown_until = time.time() + 60
        self.assertIn("breaker", self.a.unavailable_reason())
        self.a.breaker_until = 0.0
        self.assertIn("cooldown", self.a.unavailable_reason())
        self.a.cooldown_until = 0.0
        self.assertEqual(self.a.unavailable_reason(), "")


class EnterpriseSkip(unittest.TestCase):
    # hub 07d033c
    def ent(self, **extra):
        tok = make_jwt(**{k: v for k, v in extra.items() if k == "enterprise_id"}) \
            if extra else make_jwt()
        a = Account({"accessToken": tok, "uid": "e1", "realm": "cn"})
        a.enterprise_id = "ent-1"
        return a

    def test_checkin_gates(self):
        a = self.ent()
        self.assertFalse(a.can_checkin())
        self.assertIn("enterprise", a.checkin()["error"])

    def test_token_only_enterprise_detected(self):
        a = Account({"accessToken": make_jwt(enterprise_id="space-9"),
                     "uid": "e2", "realm": "cn"})
        self.assertTrue(a.is_enterprise())

    def test_public_flags(self):
        a = self.ent()
        self.assertFalse(a.public()["canCheckin"])

    def test_task_entries_skip(self):
        from gateway import tasks as T
        a = self.ent()
        self.assertEqual(T.fetch_growth_tasks(a), [])
        self.assertEqual(T.do_cat_travel(a)["action"], "skip")
        self.assertTrue(T.run_growth_tasks(a)["ok"])
        self.assertTrue(T.run_night_growth(a)["ok"])


class DailyChatWebLeg(unittest.TestCase):
    # hub 93e9f26: failed web leg must not close the day
    class FakeResp:
        def read(self, *x):
            return b"ok"
        def __enter__(self):
            return self
        def __exit__(self, *x):
            return False

    def account(self):
        d = tempfile.mkdtemp()
        a = Account({"accessToken": make_jwt(exp=time.time() + 7200),
                     "uid": "dc1", "realm": "intl"})
        a.path = os.path.join(d, "dc1.json")
        a.save(d)
        a.fetch_credits = lambda *x, **k: None
        return a

    def test_failed_web_leg_leaves_day_open(self):
        a = self.account()
        a.daily_chat_web = lambda *x, **k: {"ok": False, "error": "tunnel 407"}
        with mock.patch.object(acc_mod, "urlopen",
                               lambda *x, **k: self.FakeResp()):
            res = a.daily_chat(web=True)
        self.assertFalse(res["ok"])   # hub ddda600: web leg owns the verdict
        self.assertIsNone(a.last_daily_chat)
        self.assertTrue(a.can_daily_chat())

    def test_success_and_web_off_close_the_day(self):
        a = self.account()
        a.daily_chat_web = lambda *x, **k: {"ok": True, "status": "completed",
                                            "chunks": 2, "elapsed_ms": 5}
        with mock.patch.object(acc_mod, "urlopen",
                               lambda *x, **k: self.FakeResp()):
            res = a.daily_chat(web=True)
        self.assertTrue(a.last_daily_chat)
        self.assertFalse(a.can_daily_chat())
        b = self.account()
        with mock.patch.object(acc_mod, "urlopen",
                               lambda *x, **k: self.FakeResp()):
            res2 = b.daily_chat(web=False)
        self.assertTrue(res2["ok"])
        self.assertTrue(b.last_daily_chat)
        self.assertNotIn("web", res2)


class HotPath(unittest.TestCase):
    # hub 8bc1f0d
    def test_fingerprint_literal_path_matches_regex_semantics(self):
        import re
        cases = [
            "x-Anthropic-Billing-Header: c",
            "Sisyphus-Junior - Focused executor from OhMyOpenCode",
            "İx-anthropic-billing-header",   # re.I folds dotted I to i
            "ſisyphus-junior - focused executor from ohmyopcode",
            "ordinary text", "ı", "İ",
        ]
        bare = re.compile(r"(?i)x-anthropic-billing-header")
        omo = re.compile(r"Sisyphus-Junior - Focused executor from OhMyOpenCode",
                         re.IGNORECASE)
        for t in cases:
            legacy = bool(bare.search(t) or omo.search(t))
            low = t.translate(tr.SANITIZE_REI_FOLD).lower() \
                if any(c in t for c in "İıſ") else t.lower()
            literal = (tr.SANITIZE_BARE_HDR_LOWER in low
                       or tr.SANITIZE_OMO_JUNIOR_LOWER in low)
            self.assertEqual(legacy, literal, t)

    def test_cjk_estimate_single_pass(self):
        for t in ["中文abc", "a" * 100, "你好世界" * 10, "\u3400x"]:
            cjk_old = sum(1 for c in t
                          if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")
            self.assertEqual(cjk_old, len(t) - len(tr.CJK_RE.sub("", t)))

    def test_settings_cache_invalidation(self):
        d = tempfile.mkdtemp()
        st.set_api_key(d, "sk-1")
        self.assertEqual(st.get_api_key(d), "sk-1")
        first = st.load(d)
        first["api_key"] = "tampered"          # returned copy must not poison cache
        self.assertEqual(st.get_api_key(d), "sk-1")
        st.save(d, {"api_key": "sk-2"})
        self.assertEqual(st.get_api_key(d), "sk-2")

    def test_prebuilt_body_skips_rebuild(self):
        payload = {"model": "hy3", "messages": [{"role": "user", "content": "hi"}]}
        prebuilt = {"model": "hy3", "messages": [{"role": "user", "content": "hi"}],
                    "special": 1}
        a = FakeAccount("u1")
        with mock.patch.object(up.wb_accounts, "urlopen",
                               fake_urlopen_from([b"ok"])), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(up, "build_upstream_body") as mb:
            up.open_upstream(FakePool([a]), payload, prebuilt_body=prebuilt)
            mb.assert_not_called()


class PerfWiring(unittest.TestCase):
    # hub 3def05a + d419dab
    def test_server_socket_options(self):
        from gateway import server as srv
        self.assertTrue(srv.Handler.disable_nagle_algorithm)
        self.assertEqual(srv.GatewayServer.request_queue_size, 128)
        self.assertEqual(srv.MAX_PAYLOAD_BYTES, 16 * 1024 * 1024)
        httpd = srv.build_server("127.0.0.1", 0)
        self.assertIsInstance(httpd, srv.GatewayServer)
        httpd.server_close()

    def test_pool_enabled_by_default_and_toggleable(self):
        from gateway.upstream import pool as P
        old = os.environ.pop("WB_UPSTREAM_KEEPALIVE", None)
        try:
            self.assertTrue(P.enabled())
            os.environ["WB_UPSTREAM_KEEPALIVE"] = "0"
            self.assertFalse(P.enabled())
        finally:
            if old is None:
                os.environ.pop("WB_UPSTREAM_KEEPALIVE", None)
            else:
                os.environ["WB_UPSTREAM_KEEPALIVE"] = old

    def test_bypass_falls_back_to_urllib(self):
        from gateway.upstream import pool as P
        # non-http scheme must raise urllib's own error, never PoolBypass
        try:
            acc_mod.urlopen(urllib.request.Request("ftp://invalid/x"), timeout=1)
            self.fail("expected URLError")
        except P.PoolBypass:
            self.fail("PoolBypass leaked to caller")
        except urllib.error.URLError:
            pass

    def test_egress_key_folds_proxy_identity(self):
        from gateway.upstream import pool as P
        k1 = P.egress_key("https://api.workbuddy.ai/v1/chat", "")
        self.assertEqual(k1[0], "https")
        self.assertEqual(k1[1], "api.workbuddy.ai")
        self.assertEqual(k1, P.egress_key("https://api.workbuddy.ai/v1/chat", ""))


class GrowthPrereqChain(unittest.TestCase):
    # hub 1fd0194 + 0970a5d
    def task(self, code, status="not_accepted", current=0, target=1, task_type=""):
        return {"task_code": code, "name": code, "description": "", "jump_url": "",
                "status": status, "task_type": task_type, "current": current,
                "target": target, "reward_credit": 0, "reward_energy": 0,
                "unforgeable": False, "reason": ""}

    def test_auto_tasks_run_before_accepts(self):
        from gateway import tasks as T
        a = Account({"accessToken": make_jwt(), "uid": "g1", "realm": "cn"})
        a.credits = {"remain": 0}
        a.fetch_credits = lambda *x, **k: None
        state = {"fb": {"status": "not_accepted", "current": 0}}

        def fetch(acct):
            fb = state["fb"]
            return [self.task("first_buddy", status=fb["status"],
                              current=fb["current"], task_type="auto"),
                    self.task("chat_5", status="not_accepted")]
        accepted = []
        def accept(acct, codes, **kw):
            accepted.extend(codes)
            return {"ok": True, "accepted": list(codes), "failed": [], "msg": "",
                    "reasons": []}
        def report(acct, evs, base=None):
            state["fb"]["current"] = 1
            state["fb"]["status"] = "completed"
            return True
        claimed = []
        def claim(acct, code):
            claimed.append(code)
            if code == "first_buddy":
                state["fb"]["status"] = "claimed"
            return {"ok": True, "credit": 300}
        with mock.patch.object(T, "fetch_growth_tasks", fetch), \
             mock.patch.object(T, "accept_tasks", accept), \
             mock.patch.object(T, "report_events", report), \
             mock.patch.object(T, "claim_task", claim), \
             mock.patch.object(T, "do_cat_travel", lambda acct: {"ok": True}), \
             mock.patch.object(T, "_pause", lambda s=1.0: None):
            res = T.run_growth_tasks(a)
        self.assertIn("first_buddy", claimed)
        self.assertNotIn("first_buddy", accepted)  # auto is never accepted
        self.assertIn("chat_5", accepted)          # single accepted after prereq

    def test_travel_repairs_missing_buddy(self):
        from gateway import tasks as T
        a = Account({"accessToken": make_jwt(), "uid": "g2", "realm": "cn"})
        a.fetch_credits = lambda *x, **k: None
        calls = []
        def fake_open(req, timeout=30, proxy=""):
            u = req.full_url
            calls.append(u)
            r = mock.MagicMock()
            r.__enter__.return_value = r
            if u.endswith("/travel/status"):
                r.read.return_value = json.dumps(
                    {"code": 0, "data": {"state": "idle", "buddy_id": 0}}).encode()
            elif u.endswith("/travel/config"):
                r.read.return_value = json.dumps(
                    {"code": 0, "data": {"locations": [{"id": 1}]}}).encode()
            elif u.endswith("/travel/depart"):
                if sum(1 for c in calls if c.endswith("/depart")) == 1:
                    raise fake_http_error(400, {"msg": "no active buddy"})
                r.read.return_value = json.dumps(
                    {"code": 0, "data": {"location": {"name": "seaside"}}}).encode()
            else:
                r.read.return_value = b'{"code":0,"data":{}}'
            return r
        state = {"fb": "not_accepted"}
        def fetch(acct):
            return [self.task("first_buddy", status=state["fb"],
                              current=1 if state["fb"] != "not_accepted" else 0,
                              task_type="auto")]
        with mock.patch.object(acc_mod, "urlopen", fake_open), \
             mock.patch.object(T._accounts, "urlopen", fake_open), \
             mock.patch.object(T, "fetch_growth_tasks", fetch), \
             mock.patch.object(T, "report_events",
                               lambda acct, evs, base=None: (
                                   state.__setitem__("fb", "completed"), True)[1]), \
             mock.patch.object(T, "claim_task",
                               lambda acct, code: (state.__setitem__("fb", "claimed"),
                                                   {"ok": True, "credit": 300})[1]):
            res = T.do_cat_travel(a)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "depart")
        self.assertEqual(sum(1 for c in calls if c.endswith("/depart")), 2)


if __name__ == "__main__":
    unittest.main()
