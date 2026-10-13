"""Offline tests for gateway.upstream: rotation and throttle semantics.

A scripted fake pool/upstream stands in for the network: each account has a
list of outcomes (raise or return), and open_upstream() must classify them
exactly like the source project does.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import upstream as up  # noqa: E402
from gateway import translate as tr  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers_jwt import make_jwt  # noqa: E402


class FakeAccount:
    def __init__(self, uid, realm="intl", outcomes=None):
        self.uid = uid
        self.realm = realm
        self.enabled = True
        self.access_token = "tok"
        self.proxy = ""
        self.product = "cli"
        self.nickname = uid
        self.outcomes = list(outcomes or ["ok"])
        self.errors = []
        self.model_cooldowns = {}
        self.cooldown_until = 0.0
        self.balance_until = 0.0
        self.soft_streak = 0
        self.fails = 0
        self.degrade_count = 0
        self.breaker_until = 0.0
        self.degrade_until = 0.0
        self.last_error = ""
        self.cleared = []

    def pick_outcome(self):
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0]

    def headers(self, purpose="chat"):
        return {"Authorization": "Bearer tok"}

    def chat_base_url(self):
        return "http://fake-upstream.invalid"

    def note_error(self, msg, cooldown=60, single_account=False, model=None, until=None):
        self.errors.append({"msg": msg, "model": model, "until": until})
        if model and until:
            self.model_cooldowns[model] = until
        elif not model:
            self.cooldown_until = time.time() + cooldown

    def note_success(self, model=None):
        self.cleared.append(model)
        if model:
            self.model_cooldowns.pop(model, None)
        self.soft_streak = self.fails = self.degrade_count = 0
        self.cooldown_until = self.balance_until = 0.0
        self.breaker_until = self.degrade_until = 0.0
        self.last_error = ""

    def note_failure(self, msg):
        self.fails += 1
        self.errors.append({"msg": msg, "model": None, "until": None})
        return self.breaker_until

    def note_unknown_failure(self, msg):
        self.degrade_count += 1
        self.errors.append({"msg": msg, "model": None, "until": None})
        return self.degrade_until

    def note_soft_rate(self, msg):
        self.soft_streak += 1
        self.errors.append({"msg": msg, "model": None, "until": None})
        return 600.0

    def note_unscoped_rate(self, model):
        self.unscoped_streak = getattr(self, "unscoped_streak", 0) + 1
        self.errors.append({"msg": "unscoped 429", "model": model, "until": None})
        return 60.0 * self.unscoped_streak

    def note_balance_cooled(self, msg="insufficient credits"):
        self.balance_until = time.time() + 3600
        self.errors.append({"msg": msg, "model": None, "until": None})
        return self.balance_until

    def revive_balance_cooldown(self):
        return False

    def clear_error(self, model=None):
        self.cleared.append(model)
        if model:
            self.model_cooldowns.pop(model, None)

    def throttle_wait(self, model=None):
        return max(0.0, self.cooldown_until - time.time(),
                   self.balance_until - time.time(),
                   self.model_cooldowns.get(model, 0.0) - time.time())

    def set_product(self, value):
        if value == self.product:
            return False
        self.product = value
        return True

    def ready(self, model=None):
        return True


class FakePool:
    def __init__(self, accounts):
        self.accounts = accounts
        self.affinity = mock.MagicMock()

    def count_ready(self, realm=None, model=None):
        return len([a for a in self.accounts if not realm or a.realm == realm])

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None):
        for a in self.accounts:
            if a.realm == realm and a.uid not in (exclude or set()):
                return a
        return None

    def pick(self, realm=None, exclude=None, model=None):
        return self.pick_for_session(realm=realm, exclude=exclude, model=model)


def http_error(code, body=b""):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


def fake_urlopen_from(account_responses):
    """urlopen stub: next response from a shared iterator, honoring markers."""
    def _open(req, timeout=30, proxy=""):
        outcome = account_responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        resp = mock.MagicMock()
        resp.read.return_value = outcome
        resp.__enter__.return_value = resp
        return resp
    return _open


class Routing(unittest.TestCase):
    def test_detect_realm_and_exclusivity(self):
        self.assertEqual(tr.detect_model_realm("gpt-6-astra"), "intl")
        self.assertEqual(tr.detect_model_realm("minimax-m3"), "cn")
        self.assertEqual(up.exclusive_realm("gemini-3.5-flash"), "intl")
        self.assertEqual(up.exclusive_realm("deepseek-v4-pro"), "cn")
        self.assertEqual(up.exclusive_realm("hy3"), "")

    def test_banned_models_off_by_default(self):
        self.assertFalse(tr.is_model_banned("gpt-6-astra"))


class OpenUpstream(unittest.TestCase):
    def payload(self, model="hy3"):
        return {"model": model, "messages": [{"role": "user", "content": "hi"}]}

    def open_with(self, pool, responses, **kw):
        with mock.patch.object(up.wb_accounts, "urlopen",
                               fake_urlopen_from(responses)), \
             mock.patch.object(time, "sleep"):
            return up.open_upstream(pool, self.payload(), **kw)

    def test_success_returns_response_and_account(self):
        a = FakeAccount("u1")
        resp, acct = self.open_with(FakePool([a]), [b"data: [DONE]\n\n"])
        self.assertEqual(acct.uid, "u1")
        self.assertIn(b"[DONE]", resp.read())

    def test_401_rotates_to_next_account(self):
        a = FakeAccount("u1", outcomes=["fail"])
        b = FakeAccount("u2")
        pool = FakePool([a, b])
        resp, acct = self.open_with(pool, [http_error(401), b"ok"])
        self.assertEqual(acct.uid, "u2")
        self.assertTrue(a.errors)  # u1 was marked, not u2

    def test_429_model_scoped_with_reset_time(self):
        a = FakeAccount("u1")
        b = FakeAccount("u2")
        reset = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 3600))
        body = json.dumps({"code": 6004, "msg": "usage exceeds frequency limit; "
                          "your usage will reset at %s UTC+8" % reset}).encode()
        with self.assertRaises(up.RateLimited) as ctx:
            self.open_with(FakePool([a, b]), [http_error(429, body), http_error(429, body),
                                              http_error(429, body)])
        # cooldown landed on the model, not the whole account
        self.assertIn("hy3", a.model_cooldowns)
        self.assertEqual(a.cooldown_until, 0.0)
        self.assertGreater(ctx.exception.wait, 1)

    def test_403_passes_through_without_pool_damage(self):
        a = FakeAccount("u1")
        with self.assertRaises(up.ContentRejected) as ctx:
            self.open_with(FakePool([a]), [http_error(403, b"review blocked")])
        self.assertEqual(a.errors, [])  # pool untouched
        self.assertIn("review", ctx.exception.detail)

    def test_transient_network_retries_same_account(self):
        a = FakeAccount("u1")
        # ConnectionReset is transient: retried, no cooldown recorded.
        resp, acct = self.open_with(FakePool([a]),
                                    [ConnectionResetError("reset by peer"), b"ok"])
        self.assertEqual(acct.uid, "u1")
        self.assertEqual(a.errors, [])

    def test_dead_pool_raises_runtime(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.open_with(FakePool([]), [])
        self.assertIn("no usable account", str(ctx.exception))

    def test_all_model_cooling_maps_to_rate_limited(self):
        a = FakeAccount("u1")
        a.model_cooldowns["hy3"] = time.time() + 50
        pool = FakePool([a])
        # pick_for_session yields None (pool healthy but cooling): emulate by
        # an accounts-filtering pool
        class CoolingPool(FakePool):
            def pick_for_session(self, **kw):
                return None
        with self.assertRaises(up.RateLimited):
            self.open_with(CoolingPool([a]), [])


class RealmPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        up.set_realm("intl")
        self.tmp.cleanup()

    def test_roundtrip(self):
        up.save_persisted_realm(self.tmp.name, "cn")
        self.assertEqual(up.get_realm(), "cn")
        up.set_realm("intl")
        self.assertEqual(up.load_persisted_realm(self.tmp.name), "cn")
        self.assertEqual(up.get_realm(), "cn")

    def test_corrupt_file_tolerated(self):
        with open(os.path.join(self.tmp.name, "active_realm.json"), "w") as fh:
            fh.write("nope")
        up.save_persisted_realm(self.tmp.name, "intl")
        self.assertIn(up.load_persisted_realm(self.tmp.name), ("intl", "cn"))

    def test_translate_sees_switch(self):
        up.set_realm("cn")
        self.assertEqual(tr.CURRENT_REALM, "cn")
        self.assertEqual(tr.detect_model_realm("deepseek-v4.1-flash"), "cn")


class Catalog(unittest.TestCase):
    def test_merge_prefers_ui_order(self):
        entries = up.merge_catalog([("hy3", {"name": "live"})], realm="intl")
        ids = [m for m, _ in entries]
        self.assertIn("gpt-6-astra", ids)
        self.assertNotIn("lite", ids)
        self.assertNotIn("codewise-any", ids)
        # live metadata overlays the static entry
        self.assertEqual(dict(entries)["hy3"].get("name"), "live")

    def test_model_entry_flags(self):
        e = up.model_entry("deepseek-v4.1-flash", {"supportsImages": True,
                                                   "supportsToolCall": True,
                                                   "maxOutputTokens": 8000})
        self.assertTrue(e["capabilities"]["vision"])
        self.assertTrue(e["capabilities"]["tool_calls"])
        self.assertEqual(e["max_output_tokens"], 8000)
        self.assertEqual(e["reasoning_default_effort"], "high")

    def test_endpoint_failure_falls_back_to_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tr._models_cache["intl"] = {"at": time.time(), "data": [("cached-model", {})]}
        ids = up.fetch_endpoint_models(pool=None)
        self.assertIn("cached-model", ids)
        tr._models_cache["intl"] = {"at": 0.0, "data": None}


class Transients(unittest.TestCase):
    def test_transient_markers(self):
        self.assertTrue(up.is_transient(ConnectionResetError("reset")))
        self.assertTrue(up.is_transient(Exception("SSL: UNEXPECTED_EOF")))
        self.assertFalse(up.is_transient(Exception("bad request")))

    def test_reset_parse(self):
        d = "reset at 2026-09-19 18:29:03 UTC+8"
        epoch = up.parse_rate_limit_reset(d)
        self.assertIsInstance(epoch, float)
        self.assertIsNone(up.parse_rate_limit_reset("no reset here"))


class ModelsAllTests(unittest.TestCase):
    def test_union_with_exit_aliases(self):
        catalogs = {
            "intl": [("hy3", {"name": "HY3"}), ("gpt-5.5", {})],
            "cn": [("hy3", {"name": "HY3"}), ("glm-5.3-flash", {})],
        }
        real = up.fetch_models
        up.fetch_models = lambda realm=None, pool=None: catalogs[realm]
        up.set_realm("intl")
        try:
            out = up.fetch_models_all()
        finally:
            up.fetch_models = real
            up.set_realm("intl")
        ids = [m for m, _ in out]
        # dual-exit model: bare + both aliases; exclusive models: bare ONLY
        # (the bare id of an exclusive model already pins its exit)
        self.assertIn("hy3", ids)
        self.assertIn("cn:hy3", ids)
        self.assertIn("global:hy3", ids)
        self.assertIn("gpt-5.5", ids)
        self.assertIn("glm-5.3-flash", ids)
        for dup in ("cn:gpt-5.5", "global:gpt-5.5",
                    "cn:glm-5.3-flash", "global:glm-5.3-flash"):
            self.assertNotIn(dup, ids)
        # no duplicates, and the bare dual entry belongs to the CURRENT realm
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(dict(out)["hy3"].get("exitRealm"), "intl")
        self.assertEqual(dict(out)["cn:hy3"].get("exitRealm"), "cn")


if __name__ == "__main__":
    unittest.main(verbosity=2)
