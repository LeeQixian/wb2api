"""Tests for the v1.10.9 -> v2.0.3 selective sync: empty text parts, upstream
reasoning mirror, Responses reasoning replay, Claude Code intro, intl -sg alias,
concurrency seat release."""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from gateway import settings as st  # noqa: E402
from gateway import translate as tr  # noqa: E402
from gateway import upstream as up  # noqa: E402
from gateway import webtools as wb_webtools  # noqa: E402
from gateway.protocol.repair import (  # noqa: E402
    guard_empty_text_parts, mirror_upstream_reasoning,
)
from gateway.protocol.request import build_upstream_body  # noqa: E402
from gateway.protocol.responses_in import responses_to_chat  # noqa: E402
from gateway.protocol.responses_out import follow_up_with_tool_results  # noqa: E402
from gateway.protocol.sanitize import has_fingerprint, sanitize_text  # noqa: E402
from gateway.upstream import client as up_client  # noqa: E402
from gateway.upstream.client import (  # noqa: E402
    apply_intl_model_alias, intl_model_alias,
)


class EmptyTextParts(unittest.TestCase):
    """#840: an empty text part is a hard 400/11151, so fill it before sending."""

    def test_real_part_list_is_filled(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
        message = {"role": "user", "content": [{"type": "text", "text": ""}, image]}
        guard_empty_text_parts([message])
        self.assertEqual(message["content"][0]["text"], " ")
        self.assertIs(message["content"][1], image)

    def test_null_and_missing_text_filled(self):
        message = {"role": "user", "content": [
            {"type": "text", "text": None}, {"type": "text"}]}
        guard_empty_text_parts([message])
        self.assertEqual([p["text"] for p in message["content"]], [" ", " "])

    def test_non_empty_and_unknown_types_untouched(self):
        keep = [{"type": "text", "text": "hi"}, {"type": "textarea", "text": ""}]
        message = {"role": "user", "content": [dict(p) for p in keep]}
        guard_empty_text_parts([message])
        self.assertEqual(message["content"], keep)

    def test_sniffer_parsed_string_is_filled(self):
        message = {"role": "tool", "content":
                   '[{"type":"text","text":""},{"type":"text","text":"x"}]'}
        guard_empty_text_parts([message])
        parsed = json.loads(message["content"])
        self.assertEqual([p["text"] for p in parsed], [" ", "x"])

    def test_plain_and_non_part_strings_untouched(self):
        for content in ("", "hello", "[]", "[1, 2]", '{"type":"text","text":""}',
                        '[{"type":"text","text":""}'):
            message = {"role": "user", "content": content}
            guard_empty_text_parts([message])
            self.assertEqual(message["content"], content, content)

    def test_wired_into_outbound_body(self):
        body = build_upstream_body({
            "model": "hy3",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": ""}, {"type": "text", "text": "hi"}]}],
        })
        parts = body["messages"][-1]["content"]
        self.assertEqual(parts[0]["text"], " ")


class ReasoningMirror(unittest.TestCase):
    """#838: models whose upstream reads `reasoning` get reasoning_content mirrored."""

    def mirror(self, model, messages):
        return mirror_upstream_reasoning(messages, model)

    def test_matching_prefixes(self):
        for model in ("kimi", "kimi-k3", "glm-5.3", "minimax-m2.5", "mimo-7",
                      "step-3", "space-bunny-2"):
            msgs = self.mirror(model, [{"role": "assistant",
                                        "content": "x", "reasoning_content": "r"}])
            self.assertEqual(msgs[0]["reasoning"], "r", model)

    def test_prefix_boundary_is_strict(self):
        msgs = self.mirror("kimiya", [{"role": "assistant",
                                       "content": "x", "reasoning_content": "r"}])
        self.assertNotIn("reasoning", msgs[0])
        msgs = self.mirror("hy3", [{"role": "assistant",
                                    "content": "x", "reasoning_content": "r"}])
        self.assertNotIn("reasoning", msgs[0])

    def test_existing_reasoning_and_blank_source_untouched(self):
        msgs = self.mirror("glm-5.3", [
            {"role": "assistant", "content": "x",
             "reasoning_content": "new", "reasoning": "old"},
            {"role": "assistant", "content": "y", "reasoning_content": "   "},
            {"role": "user", "content": "z", "reasoning_content": "r"},
        ])
        self.assertEqual(msgs[0]["reasoning"], "old")
        self.assertNotIn("reasoning", msgs[1])
        self.assertNotIn("reasoning", msgs[2])

    def test_wired_into_outbound_body(self):
        body = build_upstream_body({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "ok",
                          "reasoning_content": "traced"}],
        })
        self.assertEqual(body["messages"][-1]["reasoning"], "traced")


class ResponsesReasoningReplay(unittest.TestCase):
    """#831: replayed reasoning stays attached to the right assistant turn."""

    def test_consecutive_reasoning_items_are_merged(self):
        chat = responses_to_chat({"model": "hy3", "input": [
            {"type": "reasoning",
             "summary": [{"type": "summary_text", "text": "first"}]},
            {"type": "reasoning",
             "summary": [{"type": "summary_text", "text": "second"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "done"}]},
        ]})
        self.assertEqual(chat["messages"][-1]["reasoning_content"], "first\nsecond")

    def test_reasoning_opens_its_own_assistant_turn(self):
        chat = responses_to_chat({"model": "hy3", "input": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "earlier"}]},
            {"type": "reasoning",
             "summary": [{"type": "summary_text", "text": "next trace"}]},
            {"type": "function_call", "call_id": "c1", "name": "run",
             "arguments": "{}"},
        ]})
        self.assertEqual(len(chat["messages"]), 2)
        self.assertEqual(chat["messages"][0]["content"], "earlier")
        self.assertNotIn("reasoning_content", chat["messages"][0])
        self.assertEqual(chat["messages"][1]["reasoning_content"], "next trace")
        self.assertEqual(chat["messages"][1]["tool_calls"][0]["id"], "c1")

    def test_trailing_reasoning_folds_into_previous_assistant(self):
        chat = responses_to_chat({"model": "hy3", "input": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "answer"}]},
            {"type": "reasoning",
             "summary": [{"type": "summary_text", "text": "late trace"}]},
        ]})
        self.assertEqual(chat["messages"][-1]["reasoning_content"], "late trace")

    def test_assistant_message_keeps_inline_reasoning_and_calls(self):
        chat = responses_to_chat({"model": "hy3", "input": [
            {"type": "message", "role": "assistant", "content": "",
             "reasoning_content": "why",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "run", "arguments": "{}"}}]},
        ]})
        message = chat["messages"][-1]
        self.assertEqual(message["reasoning_content"], "why")
        self.assertEqual(message["tool_calls"][0]["id"], "c1")


class ClaudeCodeIntro(unittest.TestCase):
    """4df31cf: the self-intro sentence is neutralized, not reworded."""

    def test_mixed_case_intro_is_detected(self):
        text = "you are claude code, anthropic's official cli for claude."
        self.assertTrue(has_fingerprint(text))

    def test_intro_sentence_is_neutralized(self):
        text = ("You are Claude Code, Anthropic's official CLI for Claude.\n"
                "You help with pull requests.")
        out = sanitize_text(text)
        self.assertNotIn("Claude Code", out)
        self.assertNotIn("official CLI", out)
        self.assertIn("You help with pull requests.", out)
        self.assertFalse(has_fingerprint(out))

    def test_old_reverse_injection_rule_is_gone(self):
        text = "You are Claude Code, Anthropic's official CLI tool for Claude."
        out = sanitize_text(text)
        self.assertNotIn("official CLI", out)


class IntlModelAlias(unittest.TestCase):
    """v2.0.1: the intl exit serves deepseek-v4.1-flash as the -sg build."""

    def test_alias_only_for_intl(self):
        self.assertEqual(intl_model_alias("deepseek-v4.1-flash", "intl"),
                         "deepseek-v4.1-flash-sg")
        self.assertEqual(intl_model_alias("deepseek-v4.1-flash", "cn"),
                         "deepseek-v4.1-flash")
        self.assertEqual(intl_model_alias("deepseek-v4.1-flash-sg", "intl"),
                         "deepseek-v4.1-flash-sg")
        self.assertEqual(intl_model_alias("hy3", "intl"), "hy3")

    def test_env_switch_disables(self):
        with mock.patch.dict(os.environ, {"WB_INTL_MODEL_ALIAS": "0"}):
            self.assertEqual(intl_model_alias("deepseek-v4.1-flash", "intl"),
                             "deepseek-v4.1-flash")

    def test_apply_rewrites_payload(self):
        payload = {"model": "deepseek-v4.1-flash"}
        with mock.patch.object(up, "log", lambda *a, **k: None):
            self.assertEqual(apply_intl_model_alias(payload, "intl"),
                             "deepseek-v4.1-flash-sg")
        self.assertEqual(payload["model"], "deepseek-v4.1-flash-sg")


class _FakeAffinity:
    def unbind(self, session_key):
        pass


class _FakePool:
    def __init__(self, account, directory):
        self.account = account
        self.dir = directory
        self.affinity = _FakeAffinity()
        self.accounts = [account]

    def count_ready(self, realm, model=None):
        return 1

    def pick_for_session(self, realm=None, session_key=None, exclude=None,
                         model=None):
        return self.account if not exclude else None

    def get(self, uid):
        return self.account


def _fake_account(realm="intl"):
    account = mock.MagicMock()
    account.uid, account.realm, account.proxy = "u1", realm, ""
    account.chat_base_url.return_value = "http://fake-upstream.invalid"
    account.headers.return_value = {}
    account.product = "desktop"
    return account


class OutboundWire(unittest.TestCase):
    """The intl alias reaches the request body; a leaked seat is returned."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.account = _fake_account()

    def _open(self, payload, limit=0, realm="intl"):
        pool = _FakePool(self.account, self.dir)
        captured = {}

        def fake_open(req, timeout=30, proxy=""):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            raise urllib.error.HTTPError("x", 400, "bad", {}, io.BytesIO(b""))

        patches = [
            mock.patch.object(up.wb_accounts, "urlopen", fake_open),
            mock.patch.object(tr, "log", lambda *a, **k: None),
            mock.patch.object(up, "log", lambda *a, **k: None),
            mock.patch.object(up_client, "log", lambda *a, **k: None),
            mock.patch.object(up_client.wb_settings, "account_max_concurrent",
                              lambda directory: limit),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(urllib.error.HTTPError):
                up.open_upstream(pool, payload, target_realm=realm)
        return captured

    def test_intl_request_carries_sg_id(self):
        captured = self._open({"model": "deepseek-v4.1-flash",
                               "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(captured["body"]["model"], "deepseek-v4.1-flash-sg")

    def test_cn_request_keeps_base_id(self):
        self.account.realm = "cn"
        captured = self._open({"model": "deepseek-v4.1-flash",
                               "messages": [{"role": "user", "content": "x"}]},
                              realm="cn")
        self.assertEqual(captured["body"]["model"], "deepseek-v4.1-flash")

    def test_unhandled_status_returns_the_seat(self):
        self.assertEqual(up_client.account_inflight_snapshot(), {})
        self._open({"model": "hy3", "messages": [{"role": "user", "content": "x"}]},
                   limit=1)
        self.assertEqual(up_client.account_inflight_snapshot(), {})
        # A leaked seat would make the next pick skip this account entirely.
        captured = self._open({"model": "hy3",
                               "messages": [{"role": "user", "content": "y"}]},
                              limit=1)
        self.assertEqual(captured["body"]["model"], "hy3")


class WebToolFollowUp(unittest.TestCase):
    """#831: the web-tool round replays its reasoning and unique call ids."""

    def test_round_reasoning_and_unique_ids(self):
        holder = {"base_messages": [{"role": "user", "content": "q"}],
                  "base_body": {"model": "hy3"},
                  "realm": "intl"}
        bodies = []

        def fake_open(pool, body, session_key=None, target_realm=None):
            bodies.append(body)
            return object(), object()

        with mock.patch.object(up, "open_upstream", fake_open), \
             mock.patch.object(wb_webtools, "execute", lambda *a: "result"), \
             mock.patch.object(wb_webtools, "sources_from_result", lambda r: []), \
             mock.patch.object(tr, "log", lambda *a, **k: None):
            follow_up_with_tool_results(None, [{"name": "web_search",
                                                "arguments": "{}"}],
                                        holder, "hy3", None, 1.0,
                                        reasoning_content="trace one")
            follow_up_with_tool_results(None, [{"name": "web_search",
                                                "arguments": "{}"}],
                                        holder, "hy3", None, 1.0,
                                        reasoning_content="trace two")
        assistants = [m for m in bodies[-1]["messages"] if m.get("role") == "assistant"]
        self.assertEqual(assistants[0]["reasoning_content"], "trace one")
        self.assertEqual(assistants[1]["reasoning_content"], "trace two")
        ids = [m["tool_calls"][0]["id"] for m in assistants]
        self.assertEqual(len(set(ids)), 2)


if __name__ == "__main__":
    unittest.main()
