"""Offline tests for gateway.accounts / gateway.settings / gateway.wbctl."""
import base64
import contextlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.request
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import accounts as acct_mod  # noqa: E402
from gateway import settings  # noqa: E402
from gateway import wbctl  # noqa: E402
from gateway.accounts import Account, AccountPool  # noqa: E402


def make_jwt(uid="u-123", exp=0, iss="https://www.workbuddy.ai"):
    def b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return "%s.%s.sig" % (b64({"alg": "HS256"}),
                          b64({"sub": uid, "exp": exp, "iss": iss}))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_key_means_open(self):
        self.assertIsNone(settings.get_api_key(self.dir))
        self.assertTrue(settings.check_key(self.dir, ""))
        self.assertTrue(settings.check_key(self.dir, "whatever"))

    def test_roundtrip_and_reject(self):
        settings.set_api_key(self.dir, "sk-secret")
        self.assertEqual(settings.get_api_key(self.dir), "sk-secret")
        self.assertTrue(settings.check_key(self.dir, "sk-secret"))
        self.assertFalse(settings.check_key(self.dir, "sk-wrong"))
        self.assertFalse(settings.check_key(self.dir, ""))
        settings.clear_api_key(self.dir)
        self.assertTrue(settings.check_key(self.dir, ""))

    def test_corrupt_file_is_tolerated(self):
        with open(os.path.join(self.dir, "settings.json"), "w") as fh:
            fh.write("{broken")
        self.assertEqual(settings.load(self.dir), {})
        self.assertTrue(settings.check_key(self.dir, "x"))

    def test_settings_file_is_private(self):
        settings.set_api_key(self.dir, "sk-x")
        mode = stat.S_IMODE(os.stat(os.path.join(self.dir, "settings.json")).st_mode)
        self.assertEqual(mode, 0o600)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_token_fields_parsed(self):
        jwt = make_jwt(uid="uid-a", exp=1893456000,
                       iss="https://copilot.tencent.com")
        a = Account({"accessToken": jwt})
        self.assertEqual(a.uid, "uid-a")
        self.assertEqual(a.realm, "cn")  # issuer-driven
        self.assertEqual(a.expires_at, 1893456000)
        # hub v1.6.3 (#76): the stored identity survives a restart; a file
        # without the field falls back to the desktop identity.
        self.assertEqual(a.product, acct_mod.identity.PRODUCT_DESKTOP)
        b = Account({"accessToken": jwt, "product": "cli"})
        self.assertEqual(b.product, "cli")
        self.assertFalse(hasattr(b, "saved_product"))

    def test_save_writes_private_file(self):
        a = Account({"accessToken": make_jwt(), "nickname": "nick"})
        path = a.save(self.dir)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)
        dmode = stat.S_IMODE(os.stat(self.dir).st_mode)
        self.assertEqual(dmode, 0o700)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["nickname"], "nick")
        pool = AccountPool(self.dir)
        pool.load()
        self.assertEqual([x.uid for x in pool.accounts], [a.uid])

    def test_ready_expiry_and_model_cooldown(self):
        now = time.time()
        jwt = make_jwt(exp=int(now) + 999)
        a = Account({"accessToken": jwt})
        self.assertTrue(a.ready())
        self.assertTrue(a.ready(model="hy3"))
        a.note_error("throttled", model="hy3", until=now + 60)
        self.assertFalse(a.ready(model="hy3"))
        self.assertTrue(a.ready(model="glm-5.3"))  # model-scoped only
        a.clear_error(model="hy3")
        self.assertTrue(a.ready(model="hy3"))

    def test_refresh_needs_token_and_saves(self):
        a = Account({"accessToken": make_jwt()}, path=None)
        self.assertFalse(a.refresh())  # no refresh token
        self.assertIn("no refresh token", a.last_error)
        a2 = Account({"accessToken": make_jwt(), "refreshToken": "r-1"})
        a2.save(self.dir)
        payload = json.dumps({"data": {"accessToken": make_jwt(exp=2000000000),
                                       "refreshToken": "r-2"}}).encode()
        fake = mock.MagicMock()
        fake.read.return_value = payload
        fake.__enter__.return_value = fake
        with mock.patch.object(acct_mod, "urlopen", return_value=fake):
            self.assertTrue(a2.refresh())
        self.assertEqual(a2.refresh_token, "r-2")
        self.assertEqual(a2.expires_at, 2000000000)
        with open(a2.path, encoding="utf-8") as fh:
            self.assertIn("r-2", json.load(fh)["refreshToken"])

    def test_breaker_strikes_decay_outside_fail_window(self):
        # Strikes separated by more than FAIL_WINDOW are not "consecutive":
        # isolated blips must never park the account (hub 166a02b).
        from gateway.account.backoff import FAIL_WINDOW
        a = Account({"accessToken": make_jwt(), "uid": "u1"})
        now = time.time()
        for i in range(5):
            a.last_fail_at = now - (i + 1) * (FAIL_WINDOW + 60)
            a.note_failure("blip %d" % i)
        self.assertEqual(a.fails, 1)
        self.assertEqual(a.breaker_until, 0.0)
        b = Account({"accessToken": make_jwt(), "uid": "u2"})
        for _ in range(3):
            b.note_failure("rapid")
        self.assertGreater(b.breaker_until, time.time())

    def test_soft_ladders_decay_outside_fail_window(self):
        # Same window as fails/degrades: yesterday's three 429s must not make
        # today's first one start at the fourth rung.
        from gateway.account.backoff import FAIL_WINDOW
        a = Account({"accessToken": make_jwt(), "uid": "u3"})
        for _ in range(3):
            a.note_soft_rate("HTTP 429 (account soft rate)")
        self.assertEqual(a.soft_streak, 3)
        self.assertEqual(a.note_soft_rate("HTTP 429 (account soft rate)"), 4800.0)
        a.last_soft_at = time.time() - (FAIL_WINDOW + 1)
        self.assertEqual(a.note_soft_rate("HTTP 429 (account soft rate)"), 600.0)
        self.assertEqual(a.soft_streak, 1)

        b = Account({"accessToken": make_jwt(), "uid": "u4"})
        for _ in range(3):
            b.note_unscoped_rate("deepseek-v4.1-flash")
        self.assertEqual(b.note_unscoped_rate("deepseek-v4.1-flash"), 480.0)
        b.last_soft_at = time.time() - (FAIL_WINDOW + 1)
        self.assertEqual(b.note_unscoped_rate("deepseek-v4.1-flash"), 60.0)
        self.assertEqual(b.unscoped_streak, 1)

    def test_unavailable_reason_shows_seconds_not_zero_minutes(self):
        # "0 min left" on a 42-second cooldown reads as already usable.
        a = Account({"accessToken": make_jwt(), "uid": "u5"})
        a.cooldown_until = time.time() + 42
        a.soft_streak = 3
        reason = a.unavailable_reason(model="hy3")
        self.assertIn("42s", reason)
        self.assertNotIn("0 min", reason)
        self.assertIn("#3", reason)

    def test_headers_chat_and_billing(self):
        a = Account({"accessToken": make_jwt(), "uid": "u1"})
        h = a.headers(purpose="chat")
        self.assertEqual(h["Authorization"], "Bearer " + a.access_token)
        # Official chat never sends X-Machine-ID / X-Session-ID (5.8.4 wire).
        self.assertNotIn("X-Machine-ID", h)
        self.assertIn("X-Conversation-ID", h)
        self.assertIn("X-IDE-Type", h)  # chat identity header set
        b = a.headers(purpose="billing")
        self.assertNotIn("X-IDE-Type", b)
        self.assertIn("X-CodeBuddy-Request", b)


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = AccountPool(self.tmp.name)

    def test_import_token_and_duplicate_replace(self):
        jwt = make_jwt(uid="uid-i", exp=1893456000)
        a = self.pool.import_token(jwt, refresh_token="rr", nickname="n")
        self.assertEqual(a.uid, "uid-i")
        with self.assertRaises(ValueError):
            self.pool.import_token("not-a-jwt")
        a2 = self.pool.import_token(jwt, nickname="n2")
        self.assertEqual(len(self.pool.accounts), 1)
        self.assertEqual(a2.nickname, "n2")

    def test_import_credential_real_world_shape(self):
        # literal structure of the old-proxy export:
        # account{...} + auth{accessToken,refreshToken,expiresAt,domain,realm}
        from gateway.accounts import credential_to_kwargs
        token = make_jwt(uid="real-uid", exp=1899000000,
                         iss="https://www.workbuddy.ai")  # iss lies; realm field wins
        blob = {"account": {"uid": "real-uid", "enterpriseId": "ent-7",
                            "nickname": "\u67d0\u67d0"},
                "auth": {"accessToken": token, "refreshToken": "rt-x",
                         "expiresAt": 1794571580,
                         "domain": "www.codebuddy.cn", "realm": "cn"}}
        kw = credential_to_kwargs(blob)
        self.assertEqual(kw["uid"], "real-uid")
        self.assertEqual(kw["nickname"], "\u67d0\u67d0")
        self.assertEqual(kw["enterpriseId"], "ent-7")
        self.assertEqual(kw["realm"], "cn")           # from the explicit field
        self.assertEqual(kw["domain"], "www.codebuddy.cn")
        self.assertEqual(kw["expiresAt"], 1794571580)  # file value, not jwt exp
        self.assertEqual(kw["refreshToken"], "rt-x")
        # CLI --realm still overrides the file field
        self.assertEqual(credential_to_kwargs(blob, realm="intl")["realm"], "intl")
        a = self.pool.import_credential(json.dumps(blob))
        acct = AccountPool(self.tmp.name)
        acct.load()
        self.assertEqual(acct.get("real-uid").enterprise_id, "ent-7")
        self.assertEqual(acct.get("real-uid").domain, "www.codebuddy.cn")

    def test_import_credential_all_shapes(self):
        from gateway.accounts import credential_to_kwargs
        token = make_jwt(uid="u-nested", exp=1893456000,
                         iss="https://copilot.tencent.com")
        # desktop .info: nested account/auth, enterpriseID spelled capital-ID
        info = {"account": {"uid": "u-nested", "nickname": "nicky"},
                "auth": {"accessToken": token, "refreshToken": "rt-1"}}
        kw = credential_to_kwargs(info)
        self.assertEqual(kw["realm"], "cn")
        self.assertEqual(kw["nickname"], "nicky")
        self.assertEqual(kw["refreshToken"], "rt-1")
        # flat hub accounts/*.json shape
        flat = {"uid": "u-flat", "accessToken": token, "enterpriseId": "e-9"}
        self.assertEqual(credential_to_kwargs(flat)["enterpriseId"], "e-9")
        # raw string passthrough
        self.assertEqual(credential_to_kwargs(token)["uid"], "u-nested")
        a = self.pool.import_credential(json.dumps(info))
        self.assertEqual(a.uid, "u-nested")
        self.assertEqual(a.enterprise_id, "")
        with self.assertRaises(ValueError):
            self.pool.import_credential("{}", )

    def test_pick_round_robin_and_exclude(self):
        for uid in ("u1", "u2", "u3"):
            self.pool.import_token(make_jwt(uid=uid, exp=1893456000))
        picks = [self.pool.pick(realm="intl").uid for _ in range(4)]
        self.assertEqual(picks, ["u1", "u2", "u3", "u1"])
        self.assertEqual(self.pool.pick(realm="intl", exclude={"u1", "u2"}).uid, "u3")
        self.assertIsNone(self.pool.pick(realm="cn"))

    def test_session_affinity_pinning(self):
        for uid in ("u1", "u2"):
            self.pool.import_token(make_jwt(uid=uid, exp=1893456000))
        first = self.pool.pick_for_session(realm="intl", session_key="conv-1").uid
        again = self.pool.pick_for_session(realm="intl", session_key="conv-1").uid
        self.assertEqual(first, again)

    def test_set_enabled_and_remove(self):
        a = self.pool.import_token(make_jwt(uid="u1", exp=1893456000))
        self.pool.set_enabled("u1", False)
        self.assertEqual(self.pool.count_ready(), 0)
        self.pool.set_enabled("u1", True)
        self.assertEqual(self.pool.count_ready(), 1)
        self.assertTrue(self.pool.remove("u1"))
        self.assertFalse(os.path.exists(a.path))
        self.assertFalse(self.pool.remove("nope"))

    def test_load_skips_broken_files(self):
        self.pool.import_token(make_jwt(uid="good", exp=1893456000))
        with open(os.path.join(self.tmp.name, "broken.json"), "w") as fh:
            fh.write("{nope")
        msgs = []
        pool2 = AccountPool(self.tmp.name, log=msgs.append)
        pool2.load()
        self.assertEqual([a.uid for a in pool2.accounts], ["good"])
        self.assertTrue(any("unreadable" in m for m in msgs))

    def test_count_ready_never_refreshes(self):
        # regression: /health -> count_ready used to fire a 30s-timeout
        # network refresh on any near-expiry token, hanging the probe
        soon = make_jwt(uid="stale", exp=int(time.time()) + 60)
        a = self.pool.import_token(soon, refresh_token="rt")
        calls = []
        with mock.patch.object(Account, "refresh",
                               lambda self: calls.append(1) or True):
            self.assertEqual(self.pool.count_ready(), 0)   # probe: no refresh
            self.assertEqual(calls, [])
            picked = self.pool.pick(realm="intl")          # serving: may refresh
            self.assertIs(picked, a)
            self.assertEqual(len(calls), 1)

    def test_load_preserves_live_objects_across_hot_reload(self):
        # regression: load() must not rebuild Account objects, or a losing
        # writer could persist a dead rotated refresh token.
        a = self.pool.import_token(make_jwt(uid="keep", exp=1893456000))
        a.model_cooldowns["hy3"] = time.time() + 300   # runtime state to keep
        # simulate a refresh that rotated tokens in memory ahead of the file
        a.access_token = make_jwt(uid="keep", exp=2000000000)
        a.refresh_token = "rotated-rt"
        a.expires_at = 2000000000
        self.pool.load()
        kept = self.pool.get("keep")
        self.assertIs(kept, a)                              # same object, same locks
        self.assertIs(kept._refresh_lock, a._refresh_lock)
        self.assertEqual(kept.refresh_token, "rotated-rt")  # no rollback to file
        self.assertIn("hy3", kept.model_cooldowns)          # cooldown survived
        # and a genuinely newer file is still absorbed
        kept.access_token = "old"
        kept.save(self.tmp.name)
        another = AccountPool(self.tmp.name)
        another.load()
        self.assertEqual(another.get("keep").access_token,
                         a.access_token)


class OAuthPollTests(unittest.TestCase):
    def test_poll_login_success_adds_account(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pool = AccountPool(tmp.name)
        pool.logins["st-1"] = {"created": time.time(), "platform": "CLI", "realm": "intl"}
        token = make_jwt(uid="oauth-uid", exp=1893456000)
        responses = iter([
            json.dumps({"code": 0, "data": {"accessToken": token,
                                            "refreshToken": "rt"}}).encode(),
            json.dumps({"data": {"nickname": "Nick"}}).encode(),
        ])

        def fake_urlopen(req_or_url, *a, **kw):
            body = next(responses)
            resp = mock.MagicMock()
            resp.read.return_value = body
            resp.__enter__.return_value = resp
            return resp

        with mock.patch.object(acct_mod, "urlopen", fake_urlopen), \
             mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            res = pool.poll_login("st-1")
        self.assertEqual(res["status"], "ok")
        self.assertEqual([a.uid for a in pool.accounts], ["oauth-uid"])

    def test_poll_login_pending_and_expired(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pool = AccountPool(tmp.name)
        pool.logins["st-1"] = {"created": time.time(), "platform": "CLI", "realm": "intl"}
        with mock.patch.object(acct_mod, "http_json",
                               return_value={"code": acct_mod.LOGIN_PENDING}):
            self.assertEqual(pool.poll_login("st-1")["status"], "pending")
        pool.logins["old"] = {"created": 1, "platform": "CLI", "realm": "intl"}
        self.assertEqual(pool.poll_login("old")["status"], "expired")


class WbctlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        out, err = StringIO(), StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wbctl.main(["--dir", self.dir] + argv)
        return rc, out.getvalue(), err.getvalue()

    def test_import_list_remove_flow(self):
        token = make_jwt(uid="cli-uid", exp=1893456000)
        rc, out, _ = self._run(["import", token])
        self.assertEqual(rc, 0)
        self.assertIn("cli-uid", out)
        rc, out, _ = self._run(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("cli-uid", out)
        rc, out, _ = self._run(["disable", "cli-uid"])
        self.assertEqual(rc, 0)
        pool = AccountPool(self.dir)
        pool.load()
        self.assertFalse(pool.accounts[0].enabled)
        rc, out, _ = self._run(["remove", "cli-uid"])
        self.assertEqual(rc, 0)
        rc, out, err = self._run(["remove", "ghost"])
        self.assertEqual(rc, 1)

    def test_set_clear_key(self):
        rc, out, _ = self._run(["set-key", "sk-from-cli"])
        self.assertEqual(rc, 0)
        self.assertEqual(settings.get_api_key(self.dir), "sk-from-cli")
        rc, out, _ = self._run(["clear-key"])
        self.assertEqual(rc, 0)
        self.assertIsNone(settings.get_api_key(self.dir))

    def test_import_broken_token_exits_1(self):
        rc, out, err = self._run(["import", "garbage"])
        self.assertEqual(rc, 1)
        self.assertIn("no JWT accessToken", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
