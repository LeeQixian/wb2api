"""Tests for the 413 hint body and the per-request balance estimate."""

import json
import os
import sys
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from gateway.account.model import Account  # noqa: E402
from gateway.http import base as http_base  # noqa: E402
from gateway.http import state as _state  # noqa: E402
from gateway.http import usage as http_usage  # noqa: E402
from gateway.http.errors import BodyTooLarge  # noqa: E402


class _Recorder(object):
    """Minimal stand-in: captures what _payload_or_error would send."""

    def _read_payload(self, max_bytes=None):
        raise BodyTooLarge(_state.MAX_PAYLOAD_BYTES + 1)

    _payload_or_error = http_base.HTTPBase._payload_or_error

    def __init__(self):
        self.calls = []

    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _json(self, code, body):
        self.calls.append((code, body))

    def _error(self, code, message, err_type="server_error", gateway_hint=None):
        body = {"error": {"message": message, "type": err_type, "code": code}}
        if gateway_hint:
            body["error"]["gateway_hint"] = gateway_hint
        self._json(code, body)


class PayloadTooLargeHint(unittest.TestCase):
    def test_413_carries_a_gateway_hint(self):
        # The cap is an env knob; the caller must be told which one to raise.
        rec = _Recorder()
        self.assertIsNone(rec._payload_or_error())
        code, body = rec.calls[0]
        self.assertEqual(code, 413)
        self.assertIn("payload too large", body["error"]["message"])
        self.assertIn("WB_MAX_PAYLOAD_BYTES", body["error"]["gateway_hint"])


class _FakePool(object):
    def __init__(self, account):
        self._account = account

    def get(self, uid):
        return self._account if uid == self._account.uid else None


class ConsumeCredit(unittest.TestCase):
    def test_balance_drops_and_is_clamped_at_zero(self):
        acc = Account({"uid": "u-credit", "accessToken": "t"})
        acc.credits = {"remain": 1.5, "used": 2.0, "size": 10.0}
        acc.consume_credit(0.75)
        self.assertEqual(acc.credits["remain"], 0.75)
        self.assertEqual(acc.credits["used"], 2.75)
        acc.consume_credit(99)
        self.assertEqual(acc.credits["remain"], 0.0)

    def test_unknown_or_bad_balance_is_left_alone(self):
        acc = Account({"uid": "u-credit2", "accessToken": "t"})
        acc.consume_credit(1.0)  # no credits snapshot yet
        self.assertIsNone(acc.credits)
        acc.credits = {"remain": "abc"}
        acc.consume_credit(1.0)
        self.assertEqual(acc.credits["remain"], "abc")

    def test_record_row_charges_the_served_account(self):
        # The refresher visits one account per tick, so a served request has
        # to move the cached balance itself.
        acc = Account({"uid": "u-credit3", "accessToken": "t"})
        acc.credits = {"remain": 5.0, "used": 0.0, "size": 5.0}
        saved_pool, saved_dir = _state.POOL, _state.USAGE_DIR
        tmp = os.path.join(_ROOT, "tests", ".usage-tmp-%d" % os.getpid())
        try:
            _state.POOL = _FakePool(acc)
            _state.USAGE_DIR = tmp
            http_usage.record_row({"model": "hy3", "outcome": "ok",
                                   "account": acc.uid, "credit": 0.03})
            self.assertEqual(acc.credits["remain"], 4.97)
            with open(os.path.join(tmp, "usage.jsonl"), encoding="utf-8") as fh:
                row = json.loads(fh.readline())
            self.assertEqual(row["credit"], 0.03)
        finally:
            _state.POOL, _state.USAGE_DIR = saved_pool, saved_dir
            for name in os.listdir(tmp) if os.path.isdir(tmp) else []:
                os.unlink(os.path.join(tmp, name))
            if os.path.isdir(tmp):
                os.rmdir(tmp)

    def test_reserve_guard_sees_the_estimate(self):
        # The guard compares whole credits (int(remain) <= reserve), so use a
        # balance that the estimate can actually push below the line.
        acc = Account({"uid": "u-credit4", "accessToken": "t"})
        acc.reserve_credits = 1
        acc.credits = {"remain": 2, "used": 0, "size": 10}
        self.assertFalse(acc.reserve_blocked())
        acc.consume_credit(1.5)
        self.assertTrue(acc.reserve_blocked())


if __name__ == "__main__":
    unittest.main()
