"""Offline tests for gateway.translate runtime wiring."""
import json
import sys
import unittest

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from gateway import translate as tr  # noqa: E402


class BuildUpstreamBody(unittest.TestCase):
    def test_messages_sanitized_and_roles_normalized(self):
        out = tr.build_upstream_body({"model": "m", "messages": [
            {"role": "developer", "content": "x-anthropic-billing-header: cc_version=1"},
            {"role": "user", "content": "hello"}]})
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertNotIn("cc_version", json.dumps(out))

    def test_temperature_passthrough(self):
        out = tr.build_upstream_body({"model": "m", "temperature": 0.3,
                                      "messages": [{"role": "user", "content": "a"}]})
        self.assertEqual(out["temperature"], 0.3)


class InjectCacheKey(unittest.TestCase):
    def test_respects_existing(self):
        body = {"prompt_cache_key": "client-key"}
        self.assertEqual(tr.inject_prompt_cache_key(body, "uid", "conv")
                         ["prompt_cache_key"], "client-key")

    def test_account_scoped(self):
        k1 = tr.inject_prompt_cache_key({}, "uid-a", "conv")["prompt_cache_key"]
        k2 = tr.inject_prompt_cache_key({}, "uid-b", "conv")["prompt_cache_key"]
        self.assertNotEqual(k1, k2)


class EffortAndBans(unittest.TestCase):
    def test_default_effort_from_catalog(self):
        self.assertEqual(tr.model_default_effort("deepseek-v4.1-flash"), "high")
        self.assertIsNone(tr.model_default_effort("not-a-model"))

    def test_bans_off_by_default(self):
        self.assertFalse(tr.is_model_banned("gpt-6-astra"))
        self.assertFalse(tr.is_model_banned("anything"))

    def test_ban_tables_empty_by_default(self):
        # code defaults are empty: nothing is blocked out of the box
        self.assertFalse(tr.ALLOWED_MODELS)
        self.assertEqual(tr.BANNED_SUBSTRING, "")
        self.assertFalse(tr.EXTRA_BANNED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
