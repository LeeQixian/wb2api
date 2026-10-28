"""Tests for v1.9.0 sync: 401 challenge, DSH fileref, 11140 park, 6004 cap."""
import email.message
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from gateway.account.health import auth_failure_text  # noqa: E402
from gateway.accounts import Account  # noqa: E402
from gateway.protocol.sanitize import has_fingerprint, sanitize_text  # noqa: E402
from gateway.upstream import retry  # noqa: E402

HTML_401 = "<html><title>401 Authorization Required</title></html>"
DEAD = 'Bearer realm="copilot", error="invalid_token", error_description="invalid token"'


def http_401(challenge=None):
    headers = email.message.Message()
    if challenge:
        headers["WWW-Authenticate"] = challenge
    return urllib.error.HTTPError("https://copilot.tencent.com/v2/chat", 401,
                                  "Unauthorized", headers, None)


def account(uid="cn-1", realm="cn", **extra):
    data = {"uid": uid, "accessToken": "x.y.z", "realm": realm}
    data.update(extra)
    return Account(data)


class AuthChallenge(unittest.TestCase):
    # APISIX names a dead token only in the header, behind an HTML body.
    def test_invalid_token_outranks_the_html_bail_out(self):
        message = retry.http_error_message(http_401(DEAD), HTML_401)
        self.assertTrue(auth_failure_text(message))
        self.assertLess(message.index("www-authenticate"), message.index("<html"))

    def test_a_proxy_page_without_a_challenge_stays_exempt(self):
        plain = retry.http_error_message(http_401(), HTML_401)
        self.assertEqual(plain, "HTTP 401: " + HTML_401)
        self.assertFalse(auth_failure_text(plain))
        basic = retry.http_error_message(http_401('Basic realm="staging"'), HTML_401)
        self.assertFalse(auth_failure_text(basic))

    def test_truncation_cannot_cut_the_challenge_off(self):
        message = retry.http_error_message(http_401(DEAD), "<html>" + "x" * 400, 160)
        self.assertIn("invalid_token", message)
        self.assertTrue(auth_failure_text(message))

    def test_the_challenge_is_collapsed_and_missing_headers_are_empty(self):
        headers = email.message.Message()
        headers["WWW-Authenticate"] = 'Bearer\n  realm="copilot",\t error="invalid_token"'
        self.assertEqual(retry.auth_challenge(headers),
                         'Bearer realm="copilot", error="invalid_token"')
        self.assertEqual(retry.auth_challenge(None), "")

    def test_the_dispatch_label_parks_the_account(self):
        acc = account()
        acc.note_error(retry.http_error_message(http_401(DEAD), HTML_401, 160), 60)
        self.assertTrue(acc.token_expired)
        self.assertFalse(acc.ready(allow_refresh=False))
        self.assertIn("invalid_token", acc.last_error)


class DshFileReference(unittest.TestCase):
    PROMPT = ("Tokens prefixed with @ are paths the user explicitly referenced. "
              "Use @ to point at a file.")

    def test_the_paragraph_is_rewritten_idempotently(self):
        self.assertTrue(has_fingerprint(self.PROMPT))
        once = sanitize_text(self.PROMPT)
        self.assertIn("paths that the user explicitly referenced", once)
        self.assertEqual(sanitize_text(once), once)
        self.assertFalse(has_fingerprint(once))

    def test_user_data_is_untouched(self):
        raw = "paths the user explicitly referenced"
        self.assertEqual(sanitize_text(raw), raw)


class Restricted11140(unittest.TestCase):
    def test_the_park_survives_a_restart_and_repeats_do_not_extend_it(self):
        with tempfile.TemporaryDirectory() as directory:
            acc = account("intl-1", "intl")
            acc.path = os.path.join(directory, "intl-1.json")
            until = acc.note_restricted()
            self.assertFalse(acc.ready(allow_refresh=False))
            self.assertFalse(acc.can_daily_chat())
            self.assertGreater(acc.throttle_wait(), 7000)
            acc.note_restricted()
            self.assertEqual(acc.restricted_until, until)

            with open(acc.path, encoding="utf-8") as fh:
                disk = json.load(fh)
            self.assertEqual(disk["restrictedUntil"], int(until))
            self.assertFalse(Account(disk).ready(allow_refresh=False))

            reloaded = Account(disk)
            reloaded.path = acc.path
            reloaded.clear_error()
            self.assertGreater(reloaded.restricted_until, time.time())
            reloaded.note_success()
            self.assertEqual(reloaded.restricted_until, 0.0)
            with open(acc.path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["restrictedUntil"], 0)

    def test_daily_chat_does_not_probe_a_restricted_account(self):
        acc = account("intl-2", "intl")
        acc.note_restricted()
        res = acc.daily_chat()
        self.assertTrue(res["skipped"])
        self.assertTrue(res["restricted"])


class CapMarks(unittest.TestCase):
    def test_6004_is_a_cap_and_other_429s_are_not(self):
        self.assertTrue(retry.rate_limit_is_cap('{"code":6004,"msg":"quota"}'))
        self.assertTrue(retry.rate_limit_is_cap('{"code":"6004"}'))
        self.assertFalse(retry.rate_limit_is_cap('{"code":14003}'))
        self.assertFalse(retry.rate_limit_is_cap("usage exceeds frequency limit"))

    def test_a_cap_mark_rides_the_window_through_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            acc = account("cn-1", "cn")
            acc.path = os.path.join(directory, "cn-1.json")
            reset = time.time() + 20 * 3600
            acc.note_error("HTTP 429 (model throttled)", model="deepseek-v4.1-flash",
                           until=reset, cap_at=time.time())
            row = acc.public()["modelCooldowns"][0]
            self.assertTrue(row["cap"])
            self.assertIn("capAt", row)

            with open(acc.path, encoding="utf-8") as fh:
                disk = json.load(fh)
            self.assertTrue(disk["modelWindows"][0]["cap"])
            restored = Account(disk).public()["modelCooldowns"][0]
            self.assertTrue(restored["cap"])
            self.assertEqual(restored["capAt"], row["capAt"])

    def test_a_plain_throttle_carries_no_cap(self):
        acc = account("cn-2", "cn")
        acc.note_error("HTTP 429 (model throttled)", model="hy3",
                       until=time.time() + 60)
        self.assertNotIn("cap", acc.public()["modelCooldowns"][0])


if __name__ == "__main__":
    unittest.main()
