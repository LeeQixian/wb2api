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
        self.assertEqual(a.product, acct_mod.identity.PRODUCT_CLI)
        # restart default: saved product is read but never auto-applied
        b = Account({"accessToken": jwt, "product": "workbuddy"})
        self.assertEqual(b.saved_product, "workbuddy")
        self.assertEqual(b.product, acct_mod.identity.PRODUCT_CLI)

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

    def test_headers_chat_and_billing(self):
        a = Account({"accessToken": make_jwt(), "uid": "u1"})
        h = a.headers(purpose="chat")
        self.assertEqual(h["Authorization"], "Bearer " + a.access_token)
        self.assertIn("X-Machine-ID", h)
        self.assertEqual(h["X-Machine-ID"], acct_mod.derive_id("u1", "machine"))
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
