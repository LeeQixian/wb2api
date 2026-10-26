"""Protocol-trap tests for gateway.translate stream, body and routing helpers."""
import json
import sys
import unittest

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from gateway import translate as tr  # noqa: E402


def frame(obj):
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def delta_frame(content=None, tool_calls=None, function_call=None,
                finish=None, reasoning=None, extra=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    if function_call is not None:
        delta["function_call"] = function_call
    obj = {"id": "c1", "object": "chat.completion.chunk", "model": "m",
           "choices": [{"index": 0, "delta": delta}]}
    if finish:
        obj["choices"][0]["finish_reason"] = finish
    if extra:
        obj.update(extra)
    return obj


class StripAndClean(unittest.TestCase):
    def test_strip_data_prefix_variants(self):
        self.assertEqual(tr.strip_data_prefix(b"data: {}".decode()
                                              if False else "data: {\"a\":1}"),
                         '{"a":1}')
        self.assertEqual(tr.strip_data_prefix(": heartbeat"), "")
        self.assertEqual(tr.strip_data_prefix(""), "")
        self.assertEqual(tr.strip_data_prefix("data: : keepalive"), "")
        self.assertEqual(tr.strip_data_prefix("data:data: x"), "x")

    def test_clean_chunk_drops_empty_function_call_placeholder(self):
        raw = json.dumps(delta_frame(function_call={"name": "", "arguments": ""},
                                     finish="stop"))
        cleaned = tr.clean_chunk(raw)
        self.assertNotIn("function_call", cleaned)
        self.assertIn("finish_reason", cleaned)

    def test_clean_chunk_drops_empty_delta_without_finish(self):
        raw = json.dumps(delta_frame())
        self.assertEqual(tr.clean_chunk(raw), "")

    def test_clean_chunk_keeps_content(self):
        raw = json.dumps(delta_frame(content="hi"))
        self.assertEqual(tr.clean_chunk(raw), raw)

    def test_clean_chunk_strips_empty_noise_keys(self):
        obj = delta_frame(content="hi")
        obj["choices"][0]["delta"]["refusal"] = ""
        obj["choices"][0]["delta"]["reasoning_content"] = ""
        cleaned = json.loads(tr.clean_chunk(json.dumps(obj)))
        self.assertNotIn("refusal", cleaned["choices"][0]["delta"])
        self.assertNotIn("reasoning_content", cleaned["choices"][0]["delta"])

    def test_clean_responses_frame_removes_empty_fc(self):
        obj = {"type": "response.output_item.added",
               "item": {"type": "function_call", "name": ""}}
        raw = ("event: x\ndata: " + json.dumps(obj) + "\n\n").encode()
        out = tr.clean_responses_frame(raw)
        self.assertNotIn(b'"function_call": {"name": ""}', out)


class AggregateStream(unittest.TestCase):
    def run_agg(self, frames, model="m", resp_id=None):
        lines = [f if isinstance(f, bytes) else ("data: " + json.dumps(f)
                                                 + "\n\n").encode() for f in frames]
        lines.append(b"data: [DONE]\n\n")
        return tr.aggregate_stream(iter(lines), model, resp_id)

    def test_content_and_reasoning_aggregate(self):
        out = self.run_agg([
            delta_frame(content="he"),
            delta_frame(reasoning="think"),
            delta_frame(content="llo", finish="stop"),
        ])
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["content"], "hello")
        self.assertEqual(msg["reasoning_content"], "think")
        self.assertGreaterEqual(out["usage"]["total_tokens"], 1)  # estimate fallback

    def test_usage_from_upstream_wins(self):
        out = self.run_agg([delta_frame(content="hi",
                                        extra={"usage": {"total_tokens": 9,
                                                         "completion_tokens": 4,
                                                         "prompt_tokens": 5}})])
        self.assertEqual(out["usage"]["total_tokens"], 9)

    def test_tool_call_argument_shards_merge(self):
        out = self.run_agg([
            delta_frame(tool_calls=[{"index": 0, "id": "call_1",
                                     "function": {"name": "exec", "arguments": "{\"a\""}}]),
            delta_frame(tool_calls=[{"index": 0,
                                     "function": {"arguments": ":1}"}}]),
            delta_frame(finish="tool_calls"),
        ])
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["tool_calls"][0]["id"], "call_1")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "exec")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], '{"a":1}')
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")

    def test_empty_function_call_tail_does_not_fake_tool_call(self):
        out = self.run_agg([
            delta_frame(content="done"),
            delta_frame(function_call={"name": "", "arguments": ""}, finish="tool_calls"),
        ])
        msg = out["choices"][0]["message"]
        self.assertNotIn("tool_calls", msg)
        # finish downgraded so strict clients don't wait forever
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")

    def test_usage_missing_falls_back_to_estimate(self):
        out = self.run_agg([delta_frame(content="hello world", finish="stop")])
        self.assertGreater(out["usage"]["completion_tokens"], 0)

    def test_missing_usage_estimate(self):
        out = self.run_agg([delta_frame(content="你好世界", finish="stop")])
        self.assertGreaterEqual(out["usage"]["completion_tokens"], 4)  # 1 token / cjk


class BodyTranslation(unittest.TestCase):
    def test_system_prompt_injected(self):
        out = tr.build_upstream_body({"model": "m",
                                      "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(out["messages"][0]["role"], "system")

    def test_existing_system_kept_once(self):
        out = tr.build_upstream_body({"model": "m", "messages": [
            {"role": "system", "content": "custom"},
            {"role": "user", "content": "hi"}]})
        systems = [m for m in out["messages"] if m["role"] == "system"]
        self.assertEqual(len(systems), 1)
        self.assertEqual(systems[0]["content"], "custom")

    def test_developer_role_mapped(self):
        out = tr.build_upstream_body({"model": "m", "messages": [
            {"role": "developer", "content": "sys"},
            {"role": "user", "content": "hi"}]})
        self.assertEqual(out["messages"][0]["role"], "system")

    def test_max_completion_tokens_alias(self):
        obj = {"max_completion_tokens": 120}
        tr.translate_max_completion_tokens(obj)
        self.assertEqual(obj["max_tokens"], 120)
        obj2 = {"max_completion_tokens": 120, "max_tokens": 50}
        tr.translate_max_completion_tokens(obj2)
        self.assertEqual(obj2["max_tokens"], 50)

    def test_extra_fields_pass_through(self):
        # build_upstream_body copies the payload; unknown OpenAI-ish fields
        # ride along to the upstream.
        out = tr.build_upstream_body({"model": "m",
                                      "messages": [{"role": "user", "content": "hi"}],
                                      "web_search_options": {"x": 1}})
        self.assertEqual(out["web_search_options"], {"x": 1})
        self.assertTrue(out["stream"])
        self.assertEqual(out["stream_options"], {"include_usage": True})

    def test_fingerprint_is_stable_and_text_free(self):
        msgs = [{"role": "user", "content": "secret text"}]
        fp1 = tr.prompt_fingerprint(msgs)
        fp2 = tr.prompt_fingerprint([{"role": "user", "content": "secret text"}])
        self.assertEqual(fp1["msgs_sha"], fp2["msgs_sha"])
        self.assertNotIn("secret", json.dumps(fp1))

    def test_affinity_key_prefix_stable(self):
        base = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        k1 = tr.derive_affinity_key(base)
        k2 = tr.derive_affinity_key(base + [{"role": "assistant", "content": "x"}])
        self.assertEqual(k1, k2)  # growing conversation pins same account


class ToolPairing(unittest.TestCase):
    def test_orphan_call_and_result_trimmed(self):
        msgs = [
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "tool", "tool_call_id": "ghost", "content": "x"},
        ]
        out, changed = tr.cleanup_orphan_tool_calls(msgs)
        self.assertTrue(changed)
        self.assertEqual([tc["id"] for tc in out[0]["tool_calls"]], ["a"])
        self.assertEqual(len(out), 2)

    def test_full_pair_untouched(self):
        msgs = [{"role": "assistant", "tool_calls": [{"id": "a"}]},
                {"role": "tool", "tool_call_id": "a", "content": "ok"}]
        out, changed = tr.cleanup_orphan_tool_calls(msgs)
        self.assertFalse(changed)
        self.assertIs(out, msgs)

    def test_results_repacked_after_intruder(self):
        msgs = [
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "1"},
            {"role": "developer", "content": "notice"},
            {"role": "tool", "tool_call_id": "b", "content": "2"},
        ]
        out, changed = tr.repack_tool_result_blocks(msgs)
        self.assertTrue(changed)
        self.assertEqual([m["role"] for m in out], ["assistant", "tool", "tool", "developer"])


class ReasoningBackfill(unittest.TestCase):
    def test_deepseek_backfill_with_thinking(self):
        msgs = [{"role": "assistant", "content": "x"}]
        out = tr.backfill_reasoning_content(msgs, "deepseek-v4.1-flash",
                                            thinking_enabled=True)
        self.assertIn("reasoning_content", out[0])
        self.assertTrue(out[0]["reasoning"])  # non-empty mirror

    def test_noop_for_other_models(self):
        msgs = [{"role": "assistant", "content": "x"}]
        out = tr.backfill_reasoning_content(msgs, "glm-5.3", thinking_enabled=True)
        self.assertIs(out, msgs)

    def test_has_trace_triggers_without_thinking(self):
        msgs = [{"role": "assistant", "content": "x", "reasoning": "r"}]
        out = tr.backfill_reasoning_content(msgs, "deepseek-x", thinking_enabled=False)
        self.assertEqual(out[0]["reasoning_content"], "r")


class Sanitize(unittest.TestCase):
    def test_claude_code_fingerprint_rewritten(self):
        text = "You are Claude Code, Anthropic's official CLI for Claude. cc_entrypoint=cli;"
        out = tr.sanitize_text(text)
        self.assertNotIn("cc_entrypoint", out)
        self.assertNotIn("official CLI for", out)

    def test_clean_text_untouched(self):
        self.assertEqual(tr.sanitize_text("just a normal prompt"), "just a normal prompt")

    def test_tool_call_arguments_sanitized(self):
        msgs = [{"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "bash",
                                     "arguments": "cc_entrypoint=x done"}}]}]
        out = tr.sanitize_messages(msgs)
        self.assertNotIn("cc_entrypoint", out[0]["tool_calls"][0]["function"]["arguments"])


class Dsml(unittest.TestCase):
    def test_dsml_tool_call_extracted(self):
        sample = ('<｜DSML｜tool_calls>\n<｜DSML｜invoke name="shell">\n'
                  '<｜DSML｜parameter name="command">ls</｜DSML｜parameter>\n'
                  '</｜DSML｜invoke>\n</｜DSML｜tool_calls>')
        calls, clean = tr.parse_dsml_tool_calls(sample)
        self.assertEqual(calls[0]["name"], "shell")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"command": "ls"})
        self.assertNotIn("DSML", clean)

    def test_plain_text_returns_none(self):
        calls, clean = tr.parse_dsml_tool_calls("nothing here")
        self.assertIsNone(calls)
        self.assertEqual(clean, "nothing here")


class RealmRouting(unittest.TestCase):
    def test_detect_realm(self):
        self.assertEqual(tr.detect_model_realm("gpt-6-astra"), "intl")
        self.assertEqual(tr.detect_model_realm("gemini-3.5-flash"), "intl")
        self.assertEqual(tr.detect_model_realm("minimax-m3"), "cn")
        self.assertEqual(tr.detect_model_realm("deepseek-v4-pro"), "cn")
        self.assertEqual(tr.detect_model_realm("deepseek-v4.1-flash"), tr.CURRENT_REALM)
        self.assertEqual(tr.exclusive_realm("glm-5.3"), "")  # served by both


class RealmPrefix(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(tr.split_realm_prefix("cn:hy3"), ("hy3", "cn"))
        self.assertEqual(tr.split_realm_prefix("global:hy3"), ("hy3", "intl"))
        self.assertEqual(tr.split_realm_prefix("intl:hy3"), ("hy3", "intl"))
        self.assertEqual(tr.split_realm_prefix("CN:hy3"), ("hy3", "cn"))
        self.assertEqual(tr.split_realm_prefix(" cn:hy3 ".strip()), ("hy3", "cn"))

    def test_pass_through(self):
        for m in ("hy3", "", None, "weird:model:x", "cn:", "cnbuddy:hy3"):
            mid, realm = tr.split_realm_prefix(m)
            self.assertIsNone(realm, m)
            self.assertEqual(mid, str(m or ""))


class ResponsesTranslation(unittest.TestCase):
    def test_input_items_to_messages(self):
        payload = {"model": "m", "instructions": "sys", "input": [
            {"role": "user", "content": "hi"},
            {"type": "function_call", "call_id": "c1", "name": "tool_a",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]}
        req = tr.responses_to_chat(payload)
        msgs = req["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        roles = [m.get("role") for m in msgs]
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)
        tool_msg = [m for m in msgs if m["role"] == "tool"][0]
        self.assertEqual(tool_msg["tool_call_id"], "c1")

    def test_chat_completion_to_response_object(self):
        chat_obj = {"id": "c1", "model": "m",
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": "hi",
                                             "reasoning_content": "think"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                              "total_tokens": 3}}
        resp = tr.chat_to_response(chat_obj, "m")
        self.assertEqual(resp["object"], "response")
        self.assertEqual(resp["status"], "completed")
        kinds = [item["type"] for item in resp["output"]]
        self.assertIn("reasoning", kinds)
        self.assertIn("message", kinds)
        self.assertEqual(resp["usage"]["output_tokens"], 2)

    def test_tool_calls_map_to_function_call_items(self):
        chat_obj = {"id": "c1", "model": "m",
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": None,
                                             "tool_calls": [
                                                 {"id": "call_9", "type": "function",
                                                  "function": {"name": "x",
                                                               "arguments": "{}"}}]},
                                 "finish_reason": "tool_calls"}]}
        resp = tr.chat_to_response(chat_obj, "m")
        fc = [i for i in resp["output"] if i["type"] == "function_call"][0]
        self.assertEqual(fc["call_id"], "call_9")
        self.assertEqual(fc["name"], "x")

    def test_stream_events_sequence(self):
        lines = [
            frame(delta_frame(content="A")),
            frame(delta_frame(finish="stop", extra={"usage": {"total_tokens": 5}})),
            b"data: [DONE]\n\n",
        ]
        holder = {}
        events = list(tr.stream_responses_events(iter(lines), "m", holder))
        blob = b"".join(events).decode()
        self.assertIn("response.created", blob)
        self.assertIn("response.output_text.delta", blob)
        self.assertIn("response.completed", blob)
        self.assertIsInstance(holder.get("usage") or {}, dict)

    def test_custom_tool_names(self):
        tools = [{"type": "custom", "name": "apply_patch"},
                 {"type": "function", "name": "shell"}]
        self.assertEqual(tr.custom_tool_names(tools), {"apply_patch"})


class NamespaceTools(unittest.TestCase):
    def test_expand_resolve_roundtrip(self):
        tools = [{"type": "namespace", "name": "fs", "tools": [
            {"name": "read", "parameters": {"type": "object"}}]}]
        flat, mapping = tr.expand_namespace_tools(tools)
        names = [t["name"] for t in flat if t.get("type") == "function"]
        self.assertIn("read", mapping)
        resolved, ns = tr.resolve_namespaced_name("read", mapping)
        self.assertEqual(resolved, "read")
        self.assertTrue(ns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
