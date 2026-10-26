"""Tests for tool-choice handling, account guards, identity and translation edge cases."""
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from gateway import settings as st  # noqa: E402
from gateway import translate as tr  # noqa: E402
from gateway import upstream as up  # noqa: E402
from gateway import identity  # noqa: E402
from gateway.accounts import Account, AccountPool  # noqa: E402
from helpers_jwt import make_jwt  # noqa: E402

TOOLS = [{"type": "function", "function": {
    "name": "run_shell", "description": "d",
    "parameters": {"type": "object", "properties": {}}}}]


def _restore_usage(saved_usage, saved_cache):
    up.USAGE_DIR = saved_usage
    up._daily_usage.clear()
    up._daily_usage.update(saved_cache)


class ToolChoiceNone(unittest.TestCase):
    def test_string_none_keeps_tools(self):
        obj = {"model": "deepseek-v4.1-flash", "tools": json.loads(json.dumps(TOOLS)),
               "tool_choice": "none"}
        tr.normalize_tool_choice(obj)
        self.assertEqual(len(obj["tools"]), 1)
        self.assertEqual(obj["tool_choice"], "none")

    def test_object_none_keeps_tools(self):
        obj = {"model": "m", "tools": json.loads(json.dumps(TOOLS)),
               "tool_choice": {"type": "none"}}
        tr.normalize_tool_choice(obj)
        self.assertEqual(len(obj["tools"]), 1)
        self.assertEqual(obj["tool_choice"], "none")


class SanitizeOmO(unittest.TestCase):
    FP = "You are Sisyphus-Junior - Focused executor from OhMyOpenCode."

    def test_fingerprint_detected_and_rewritten(self):
        self.assertTrue(tr.has_fingerprint(self.FP))
        out = tr.sanitize_text(self.FP)
        self.assertNotIn("Focused executor from OhMyOpenCode", out)
        self.assertIn("Sisyphus-Junior - Focused executor", out)

    def test_lone_tokens_untouched(self):
        for text in ("Sisyphus-Junior says hi", "built with OhMyOpenCode"):
            self.assertEqual(tr.sanitize_text(text), text)


class CompactionNotBlocked(unittest.TestCase):
    def test_user_compaction_passes(self):
        payload = {"client_metadata": {"thread_source": "user",
                                       "request_kind": "compaction"}}
        self.assertEqual(tr.background_request_reason(payload), "")

    def test_background_compaction_still_blocked(self):
        payload = {"client_metadata": {"thread_source": "memory",
                                       "request_kind": "compaction"}}
        self.assertNotEqual(tr.background_request_reason(payload), "")


def _frames(chunks):
    out = []
    for c in chunks:
        out.append(("data: " + json.dumps(c) + "\n\n").encode())
    out.append(b"data: [DONE]\n\n")
    return out


class StreamItemId(unittest.TestCase):
    def test_function_call_events_carry_item_id(self):
        holder = {"usage": None}
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function":
                 {"name": "run_shell", "arguments": "{\"c\":1}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        frames = b"".join(tr.stream_responses_events(
            iter(_frames(chunks)), "m", holder))
        self.assertIn(b'"item_id": "fc_', frames)

    def test_private_markers_never_reach_upstream(self):
        body = tr.build_upstream_body({"model": "m", "_namespace_map": {"a": "b"},
                                       "_web_tools": True,
                                       "messages": [{"role": "user", "content": "hi"}]})
        self.assertFalse([k for k in body if str(k).startswith("_")])


class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _account(self):
        return Account({"accessToken": make_jwt(uid="u1"), "uid": "u1",
                        "realm": "intl"})

    def test_reserve_parks_at_or_below_balance(self):
        a = self._account()
        a.credits = {"remain": 40}
        self.assertFalse(a.reserve_blocked())
        a.reserve_credits = 50
        self.assertTrue(a.reserve_blocked())
        self.assertFalse(a.ready())

    def test_unknown_credits_never_park(self):
        a = self._account()
        a.reserve_credits = 50
        self.assertFalse(a.reserve_blocked())
        self.assertTrue(a.ready())

    def test_daily_limit_parks_until_counted(self):
        a = self._account()
        a.daily_token_limit = 1000
        self.assertFalse(a.daily_limit_blocked())   # None -> unknown, usable
        a.daily_tokens_today = 1000
        self.assertTrue(a.daily_limit_blocked())
        self.assertFalse(a.ready())

    def test_pool_resolves_reserve_from_settings(self):
        st.set_reserve_credits(self.dir, 66)
        pool = AccountPool(self.dir)
        a = self._account()
        pool.accounts.append(a)
        pool.apply_reserve_credits()
        self.assertEqual(a.reserve_credits, 66)
        # an Account read back from the saved file inherits the guard on load
        a.path = a.save(self.dir)
        pool2 = AccountPool(self.dir)
        pool2.load()
        self.assertEqual(pool2.accounts[0].reserve_credits, 66)

    def test_all_parked_maps_to_rate_limited(self):
        # The guard re-folds usage.jsonl per request, so park the pool by
        # writing today's spend, not by poking the in-memory counter.
        st.set_daily_token_limit(self.dir, 10)
        usage_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, usage_dir, True)
        row = {"at": __import__("time").time(), "account": "u1",
               "total_tokens": 999, "outcome": "ok", "model": "hy3"}
        with open(os.path.join(usage_dir, "usage.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        pool = AccountPool(self.dir)
        a = self._account()
        a.path = a.save(self.dir)
        pool.load()
        saved_usage, saved_cache = up.USAGE_DIR, dict(up._daily_usage)
        up.USAGE_DIR = usage_dir
        up._daily_usage.update({"day": "", "totals": None, "offset": 0,
                                "at": 0.0})
        self.addCleanup(_restore_usage, saved_usage, saved_cache)
        with mock.patch.object(up.wb_accounts, "urlopen",
                               side_effect=AssertionError("must not reach network")):
            with self.assertRaises(up.RateLimited) as cm:
                up.open_upstream(pool, {"model": "hy3", "messages": [
                    {"role": "user", "content": "hi"}]}, target_realm="intl")
        self.assertIn("today's token limit", cm.exception.message)
        self.assertGreaterEqual(cm.exception.wait, 60)


class Identity(unittest.TestCase):
    def test_three_products_and_desktop_default(self):
        self.assertEqual(identity.normalize_product(None), "workbuddy")
        self.assertEqual(identity.normalize_product("ide"), "vscode")
        self.assertEqual(identity.normalize_product("codebuddy"), "cli")
        # intl CLI rides the workbuddy.ai edge (codebuddy.ai does not resolve)
        self.assertEqual(identity.endpoint_for("intl", "cli")[0],
                         "https://www.workbuddy.ai")
        h = identity.build_identity_headers("workbuddy", "intl", "u", "t")
        self.assertEqual(h["X-IDE-Type"], "WorkBuddy")
        self.assertEqual(h["X-Product"], "SaaS")

    def test_account_identity_persisted(self):
        a = Account({"accessToken": make_jwt(uid="u1"), "uid": "u1"})
        self.assertEqual(a.product, "workbuddy")
        a.set_product("cli")
        self.assertEqual(a.product, "cli")

    def test_grok_is_intl_only(self):
        self.assertEqual(tr.detect_model_realm("grok-4.7"), "intl")
        self.assertEqual(up.exclusive_realm("grok-4.7"), "intl")
        self.assertEqual(up.exclusive_realm("glm-5.3-flash"), "")
        self.assertEqual(up.exclusive_realm("kimi-k2.8-preview"), "")


class WebToolsSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self._saved = tr.ACCOUNTS_DIR
        tr.ACCOUNTS_DIR = self.dir

    def tearDown(self):
        tr.ACCOUNTS_DIR = self._saved

    def test_off_by_default_declaration_passthrough(self):
        payload = {"model": "m", "input": [],
                   "tools": [{"type": "web_search"}]}
        chat = tr.responses_to_chat(payload)
        self.assertEqual(chat["tools"], [{"type": "web_search"}])
        self.assertNotIn("_web_tools", chat)
        self.assertFalse(tr.web_tools_active(chat))

    def test_on_swaps_to_gateway_function(self):
        st.set_local_web_tools(self.dir, True)
        payload = {"model": "m", "input": [],
                   "tools": [{"type": "web_search"}, {"type": "web_fetch"},
                             {"type": "function", "function": {"name": "keepme"}}]}
        chat = tr.responses_to_chat(payload)
        self.assertTrue(tr.web_tools_active(chat))
        names = [t.get("name") or (t.get("function") or {}).get("name")
                 for t in chat["tools"]]
        self.assertEqual(sorted(names), ["keepme", "web_fetch", "web_search"])

    def test_internal_calls_only_for_own_tools(self):
        chat_obj = {"choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "web_search", "arguments": "{}"}}]}}]}
        self.assertEqual(tr.internal_calls_from_chat(chat_obj, web_tools=False), [])
        calls = tr.internal_calls_from_chat(chat_obj, web_tools=True)
        self.assertEqual(calls, [{"name": "web_search", "arguments": "{}"}])

    def test_sum_usage_folds_rounds(self):
        total = tr.sum_usage(None, {"prompt_tokens": 10, "total_tokens": 12,
                                    "completion_tokens_details": {"reasoning_tokens": 2}})
        total = tr.sum_usage(total, {"prompt_tokens": 5, "total_tokens": 7,
                                     "completion_tokens_details": {"reasoning_tokens": 1}})
        self.assertEqual(total["prompt_tokens"], 15)
        self.assertEqual(total["completion_tokens_details"]["reasoning_tokens"], 3)

    def test_build_citations_only_tool_urls(self):
        text = "See https://example.com/a and https://made.up/ll"
        src = [{"url": "https://example.com/a", "title": "A"}]
        anns = tr.build_citations(text, src)
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0]["url"], "https://example.com/a")
        self.assertEqual(anns[0]["start_index"], text.index("https://example.com/a"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

class v1617Fixes(unittest.TestCase):
    """Tests for live error state, output caps, tool-call merging and timeouts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self._saved = tr.ACCOUNTS_DIR
        tr.ACCOUNTS_DIR = self.dir

    def tearDown(self):
        tr.ACCOUNTS_DIR = self._saved

    # ---- live error state never comes off disk ----
    def test_stale_error_not_resurrected(self):
        jwt = make_jwt(uid="u1", iss="https://www.workbuddy.ai")
        a = Account({"accessToken": jwt, "uid": "u1",
                    "lastError": "HTTP 429 (model throttled)",
                    "cooldownUntil": time.time() + 3600})
        self.assertEqual(a.last_error, "")
        self.assertEqual(a.cooldown_until, 0.0)
        path = a.save(self.dir)
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertNotIn("lastError", stored)
        self.assertNotIn("cooldownUntil", stored)
        self.assertIn("accessToken", stored)      # credential still lands

    # ---- catalog default output cap ----
    def test_max_tokens_defaults_from_catalog(self):
        declared = tr.model_default_max_output_tokens("hy3")
        body = tr.build_upstream_body(
            {"model": "hy3", "messages": [{"role": "user", "content": "x"}]})
        if declared:
            self.assertEqual(body.get("max_tokens"), declared)
        explicit = tr.build_upstream_body(
            {"model": "hy3", "max_tokens": 55,
             "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(explicit["max_tokens"], 55)

    def test_gpt_max_tokens_floor(self):
        # Upstream GPT-family rejects max_output_tokens below 16.
        clamped = tr.build_upstream_body(
            {"model": "gpt-6-astra", "max_tokens": 1,
             "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(clamped["max_tokens"], 16)
        kept = tr.build_upstream_body(
            {"model": "gpt-6-astra", "max_tokens": 32,
             "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(kept["max_tokens"], 32)
        # Other families keep the caller's value untouched.
        other = tr.build_upstream_body(
            {"model": "hy3", "max_tokens": 1,
             "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(other["max_tokens"], 1)

    # ---- split batches merge back, truncated args drop ----
    def test_adjacent_tool_calls_merge(self):
        msgs = [{"role": "assistant", "content": "thinking",
                 "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c2", "function": {"name": "g", "arguments": "{}"}}]}]
        out, changed = tr.merge_adjacent_tool_calls(msgs)
        self.assertTrue(changed)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["tool_calls"]), 2)
        self.assertEqual(out[0]["content"], "thinking")

    def test_truncated_arguments_detection(self):
        self.assertTrue(tr.is_truncated_arguments('{"q": tr'))
        self.assertFalse(tr.is_truncated_arguments('{"q": "x"}'))
        self.assertFalse(tr.is_truncated_arguments(""))   # legal no-arg tool
        self.assertFalse(tr.is_truncated_arguments(None))

    def test_stream_drops_truncated_call(self):
        def f(o):
            return ("data: " + json.dumps(o) + "\n\n").encode()
        frames = [f({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "shell",
                                                  "arguments": "{\"cmd"}}]}}]}),
                  f({"choices": [{"delta": {}, "finish_reason": "length"}]})]
        data = b"".join(tr.stream_responses_events(iter(frames), "m", {"usage": None}))
        self.assertNotIn(b"function_call_arguments.done", data)
        self.assertIn(b'"incomplete"', data)

    # ---- zero aliases never shadow a real hit ----
    def test_cache_alias_normalization(self):
        usage = {"prompt_tokens": 100, "total_tokens": 110,
                 "prompt_tokens_details": {"cached_tokens": 80},
                 "cache_read_input_tokens": 0, "cached_tokens": 0}
        tr.normalize_usage_cache_aliases(usage)
        self.assertEqual(usage["cache_read_input_tokens"], 80)
        self.assertEqual(usage["cached_tokens"], 80)
        resp = tr._responses_usage(usage)
        self.assertEqual(resp["input_tokens_details"]["cached_tokens"], 80)

    # ---- penalty windows and recovery ----
    def test_breaker_and_soft_rate_windows_block_pick(self):
        a = Account({"accessToken": make_jwt(uid="u1"), "uid": "u1"})
        a.note_failure("HTTP 502"); a.note_failure("HTTP 502")
        self.assertTrue(a.ready())                      # below threshold: no breaker
        a.note_failure("HTTP 502")                      # 3rd: breaker trips
        self.assertTrue(a.breaker_until > time.time())
        a.note_success()
        self.assertEqual(a.fails, 0)
        self.assertEqual(a.breaker_until, 0.0)
        b = Account({"accessToken": make_jwt(uid="u2"), "uid": "u2"})
        wait = b.note_soft_rate("HTTP 429 account rate limit reached")
        self.assertGreaterEqual(wait, 600.0)
        self.assertFalse(b.ready())
        b.note_success()
        self.assertTrue(b.ready())

    def test_account_level_429_classified_by_wording(self):
        from gateway import upstream as up
        self.assertTrue(up.rate_limit_is_account_level(
            "HTTP 429 account rate limit reached", None))
        self.assertFalse(up.rate_limit_is_account_level(
            "usage exceeds frequency limit, reset at 2026-10-09 08:00:00", 9e9))

    # ---- 402 parks until 04:00, balance revival lifts it ----
    def test_402_park_and_revive(self):
        a = Account({"accessToken": make_jwt(uid="u1"), "uid": "u1"})
        until = a.note_balance_cooled("HTTP 402 (insufficient credits)")
        self.assertGreater(until, time.time())
        self.assertLess(until - time.time(), 30 * 3600)
        self.assertFalse(a.ready())
        a.credits = {"remain": 0}
        self.assertFalse(a.revive_balance_cooldown())   # still empty
        a.credits = {"remain": 12}
        self.assertTrue(a.revive_balance_cooldown())
        self.assertEqual(a.balance_until, 0.0)
        self.assertTrue(a.ready())

    # ---- header/idle timeouts and their defaults ----
    def test_upstream_timeout_defaults_and_override(self):
        import io as _io
        from gateway import upstream as up
        self.assertEqual(up.upstream_timeouts(self.dir), (120.0, 300.0))
        st.set_upstream_timeouts(self.dir, 30, 45)
        self.assertEqual(up.upstream_timeouts(self.dir), (30.0, 45.0))
        saved = st.load(self.dir).get("upstream")
        self.assertEqual(saved["header_timeout_seconds"], 30)

    def test_idle_timeout_applied_to_socket(self):
        from gateway import upstream as up
        class FakeSock:
            def __init__(self): self.timeout = None
            def settimeout(self, v): self.timeout = v
        sock = FakeSock()
        resp = mock.MagicMock()
        resp.fp.raw._sock = sock
        self.assertTrue(up._apply_stream_idle_timeout(resp, 45))
        self.assertEqual(sock.timeout, 45.0)

    def test_open_upstream_uses_header_timeout(self):
        from gateway import upstream as up
        st.set_upstream_timeouts(self.dir, 11, 22)
        pool = mock.MagicMock()
        pool.dir = self.dir
        account = mock.MagicMock()
        account.uid, account.realm, account.proxy = "u1", "cn", ""
        account.chat_base_url.return_value = "http://fake-upstream.invalid"
        pool.pick_for_session.return_value = account
        pool.count_ready.return_value = 1
        captured = {}
        def fake_open(req, timeout=30, proxy=""):
            captured["timeout"] = timeout
            raise urllib.error.HTTPError("x", 599, "stop", {}, io.BytesIO(b""))
        with mock.patch.object(up.wb_accounts, "urlopen", fake_open), \
             mock.patch.object(tr, "log", lambda *a, **k: None), \
             mock.patch.object(up, "log", lambda *a, **k: None):
            with self.assertRaises(urllib.error.HTTPError):
                up.open_upstream(pool, {"model": "hy3", "messages": [
                    {"role": "user", "content": "x"}]}, target_realm="cn")
        self.assertEqual(captured["timeout"], 11.0)

    # ---- expiring-credits dispatch preference ----
    def _acct(self, uid, days_left=None):
        a = Account({"accessToken": make_jwt(uid=uid, iss="https://www.workbuddy.ai"),
                     "uid": uid, "realm": "intl", "enabled": True})
        if days_left is not None:
            a.credits = {"remain": 100, "updated_at": time.time(), "packages": [
                {"package_code": "c", "remain": 100, "days_left": days_left,
                 "is_expired": False, "no_expiry": False}]}
        return a

    def test_expiring_window_zero_keeps_plain_roundrobin(self):
        pool = AccountPool(self.dir)
        a, b = self._acct("u1", 1), self._acct("u2")
        pool.accounts = [a, b]
        pool.apply_expiring_window(0)         # disabled: plain round-robin
        self.assertFalse(a.in_expiring_window())
        self.assertEqual(pool.pick(realm="intl").uid, "u1")
        self.assertEqual(pool.pick(realm="intl").uid, "u2")

    def test_expiring_account_served_first(self):
        pool = AccountPool(self.dir)
        plain, urgent = self._acct("plain"), self._acct("urgent", days_left=2)
        pool.accounts = [plain, urgent]
        pool.apply_expiring_window(7)
        for _ in range(8):
            self.assertEqual(pool.pick(realm="intl").uid, "urgent")

    def test_no_expiry_packages_never_urgent(self):
        a = self._acct("u1")
        a.expiring_window_days = 7
        a.credits = {"remain": 5, "packages": [
            {"package_code": "c", "remain": 5, "days_left": 400,
             "is_expired": False, "no_expiry": True}]}
        self.assertIsNone(a.soonest_expiring_days())
        self.assertFalse(a.in_expiring_window())

    # ---- realm evidence, never a blind intl guess ----
    def test_realm_evidence_and_domain_alignment(self):
        from gateway import accounts as ac
        cn_jwt = make_jwt(uid="u1", iss="https://copilot.tencent.com")
        self.assertEqual(ac.realm_evidence(cn_jwt), "cn")
        self.assertEqual(ac.realm_evidence(make_jwt(uid="u2",
                                     iss="https://www.workbuddy.ai")), "intl")
        self.assertIsNone(ac.realm_evidence(make_jwt(uid="u3", iss="https://x.invalid")))
        # token evidence beats a stale intl domain, and the domain realigns
        a = Account({"accessToken": cn_jwt, "uid": "u1", "domain": "www.workbuddy.ai"})
        self.assertEqual(a.realm, "cn")
        self.assertEqual(a.domain, "copilot.tencent.com")
        kwargs = ac.credential_to_kwargs({"auth": {"accessToken": cn_jwt,
                                                   "domain": "www.workbuddy.ai"},
                                         "account": {"uid": "u1"}})
        self.assertEqual(kwargs["realm"], "cn")

    # ---- credits refresher ----
    def test_refresher_refreshes_stalest_account_once(self):
        from gateway.accounts import CreditsRefresher
        pool = AccountPool(self.dir)
        a = self._acct("stale-u")
        a.path = a.save(self.dir)
        b = self._acct("fresh-u")
        b.credits = {"remain": 1, "updated_at": time.time()}
        b.path = b.save(self.dir)
        pool.load()
        calls = []
        def fake_fetch(account_self):
            calls.append(account_self.uid)
            account_self.credits = {"remain": 9, "updated_at": time.time()}
            return {"ok": True, "credits": account_self.credits}
        # patch.object must be started, or it never applies; never
        # setattr+delattr on the class either, as that erases the real method.
        r = CreditsRefresher(pool, interval_seconds=60)
        st.set_credits_refresh_hours(self.dir, 12)
        with mock.patch.object(Account, "fetch_credits", fake_fetch):
            got = r.refresh_once()
        self.assertEqual(calls, ["stale-u"])
        self.assertEqual(got, "stale-u")
        st.set_credits_refresh_hours(self.dir, 0)
        with mock.patch.object(Account, "fetch_credits", fake_fetch):
            self.assertIsNone(r.refresh_once())   # disabled

    # ---- gateway system-prompt modes ----
    def test_prompt_modes(self):
        def body(**kw):
            m = {"model": "hy3", "messages": [{"role": "user", "content": "x"}]}
            m.update(kw)
            return tr.build_upstream_body(m)
        p = body()   # default passthrough
        self.assertEqual(p["messages"][0]["content"], "You are a helpful assistant.")
        p = tr.build_upstream_body({"model": "hy3", "messages": [
            {"role": "system", "content": "CLIENT"},
            {"role": "user", "content": "x"}]})
        self.assertEqual(p["messages"][0]["content"], "CLIENT")
        st.set_prompt_config(self.dir, mode="append")
        p = tr.build_upstream_body({"model": "hy3", "messages": [
            {"role": "system", "content": "CLIENT"},
            {"role": "user", "content": "x"}]})
        self.assertEqual([m["role"] for m in p["messages"]][:2], ["system", "system"])
        self.assertEqual(p["messages"][1]["content"],
                         tr.wb_prompt.DEFAULT_PROMPT)
        st.set_prompt_config(self.dir, mode="custom")
        p = tr.build_upstream_body({"model": "hy3", "messages": [
            {"role": "system", "content": "CLIENT"},
            {"role": "user", "content": "x"}]})
        self.assertNotIn("CLIENT", json.dumps(p["messages"]))
        self.assertEqual(p["messages"][0]["role"], "system")
        # an unreadable file fails open to the built-in prompt
        st.set_prompt_config(self.dir, mode="custom", file="/nonexistent/prompt.txt")
        p = body()
        self.assertEqual(p["messages"][0]["content"],
                         tr.wb_prompt.DEFAULT_PROMPT)
        with self.assertRaises(ValueError):
            st.set_prompt_config(self.dir, mode="bogus")
