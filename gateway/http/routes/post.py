"""POST routes: /realm, /v1/chat/completions and /v1/responses."""

import json
import time
import urllib.error
from urllib.parse import urlparse

from gateway import settings, translate, upstream, webtools as wb_webtools
from gateway.upstream import retry
from gateway.common.log import log
from gateway.http import state as _state
from gateway.http.errors import BadJSON, BodyTooLarge
from gateway.http.usage import (_usage_fields, extract_session_key,
                                record_row)


class PostRoutes(object):
    """POST handlers mixed into the request handler."""

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/realm", "/realm/set"):
            if not self._authorized():
                return
            payload = self._payload_or_error()
            if payload is None:
                return
            realm = str(payload.get("realm") or "").strip().lower()
            if realm not in ("intl", "cn"):
                return self._error(400, "realm must be 'intl' or 'cn'",
                                   "invalid_request_error")
            upstream.save_persisted_realm(_state.ACCOUNTS_DIR, realm)
            upstream.set_realm(realm)
            return self._json(200, {"current": realm})
        if path not in ("/v1/chat/completions", "/chat/completions",
                        "/v1/completions", "/completions",
                        "/v1/responses", "/responses",
                        "/v1/responses/compact", "/responses/compact"):
            return self._error(404, "not found", "invalid_request_error")
        if not self._authorized():
            return
        payload = self._payload_or_error()
        if payload is None:
            return
        try:
            aliases = settings.model_aliases(_state.ACCOUNTS_DIR)
        except Exception:
            aliases = {}
        translate.apply_model_aliases(payload, aliases)
        # Upstream chat calls can hold this thread for up to 600s; bound the
        # in-flight count so a few slow clients cannot exhaust the pool.
        if not _state._chat_slots.acquire(timeout=_state.CHAT_SLOT_WAIT_SECONDS):
            return self._error(503, "gateway is at its concurrent chat limit "
                                    "(%d in flight); retry shortly" % _state.MAX_CONCURRENT_CHAT)
        try:
            if path in ("/v1/responses", "/responses",
                        "/v1/responses/compact", "/responses/compact"):
                return self._handle_responses(payload)
            return self._dispatch_chat_post(payload)
        finally:
            _state._chat_slots.release()


    def _pin_realm(self, prefix_realm):
        """Effective per-request exit: model prefix > X-Realm/?realm=.
        None defers to model-based auto-routing; recomputed per request."""
        # None only when both are absent: _pinned_realm must never survive
        # into an unrelated keep-alive request.
        explicit = self._request_realm()
        self._pinned_realm = prefix_realm or explicit


    def _split_model(self, raw_model):
        model, prefix_realm = translate.split_realm_prefix(raw_model)
        if prefix_realm:
            self._pin_realm(prefix_realm)
        else:
            self._pin_realm(None)
        return model, prefix_realm


    def _dispatch_chat_post(self, payload):
        requested = str(payload.get("model") or "hy4-preview")
        model, prefix_realm = self._split_model(requested)
        if prefix_realm:
            payload["model"] = model
        blocked = self._realm_guard(model)
        if blocked:
            return self._error(400, blocked, "invalid_request_error")
        forwarded = translate.build_upstream_body(payload)
        log("chat: model=%s stream=%s msgs=%d"
            % (requested, bool(payload.get("stream")),
               len(forwarded.get("messages") or [])))
        session_key = extract_session_key(self.headers, payload)
        fp = translate.prompt_fingerprint(forwarded.get("messages"))
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        try:
            upstream_resp, account = self.open_upstream(payload, session_key,
                                                        prebuilt=forwarded)
        except Exception as exc:
            return self._upstream_error(requested, exc, t_start, fp)
        with upstream_resp:
            if want_stream:
                def reopen():
                    # Same account and egress; only the TCP connection is new.
                    return self.open_upstream(payload, session_key,
                                              prebuilt=forwarded, pin_account=account)
                return self._stream_chat(upstream_resp, requested, fp, account, t_start,
                                         reopen=reopen)
            return self._nonstream_chat(upstream_resp, requested, fp, account, t_start)


    def _handle_responses(self, payload):
        requested = str(payload.get("model") or "deepseek-v4.1-flash")
        # The /responses/compact path counts as compaction too.
        is_compact_path = self.path.split("?")[0].rstrip("/").endswith("/responses/compact")
        compaction = is_compact_path or translate.has_compaction_trigger(payload)
        model, prefix_realm = self._split_model(requested)
        if prefix_realm:
            payload["model"] = model
        if payload.get("previous_response_id") and not compaction:
            self._handle_expect_continue()
            self._discard_body()
            return self._json(400, {"error": {
                "message": "previous_response_id is not supported: this gateway "
                           "stores no response state. Send the full conversation "
                           "in 'input' instead.",
                "type": "invalid_request_error", "code": 400}})
        blocked = self._realm_guard(model)
        if blocked:
            return self._error(400, blocked, "invalid_request_error")
        session_key = extract_session_key(self.headers, payload)
        # Outbound and return path must see the same merged tool list.
        all_tools = payload.get("tools")
        all_tools = list(all_tools) if isinstance(all_tools, list) else []
        all_tools.extend(translate.additional_tools_from_input(payload))
        custom_names = translate.custom_tool_names(all_tools)
        chat_req = translate.responses_to_chat(payload, compaction=compaction)
        ns_map = chat_req.pop("_namespace_map", None)
        request_meta = {
            "tools": all_tools,
            "tool_choice": payload.get("tool_choice", "auto"),
            "parallel_tool_calls": payload.get("parallel_tool_calls", True),
        }
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        fp = translate.prompt_fingerprint(chat_req.get("messages"))
        log("responses: model=%s stream=%s msgs=%d custom_tools=%s compaction=%s"
            % (requested, want_stream, len(chat_req.get("messages") or []),
               sorted(custom_names) or "-", compaction))
        req_realm = getattr(self, "_pinned_realm", None)
        try:
            upstream_resp, account = self.open_upstream(chat_req, session_key)
        except Exception as exc:
            return self._upstream_error(requested, exc, t_start, fp)
        with upstream_resp:
            if compaction:
                return self._responses_compaction_response(
                    upstream_resp, requested, fp, account, t_start, want_stream)
            if want_stream:
                return self._stream_responses(
                    upstream_resp, requested, custom_names, request_meta, fp, account,
                    t_start, ns_map, base_body=chat_req, session_key=session_key,
                    realm=req_realm)
            return self._nonstream_responses(
                upstream_resp, requested, custom_names, request_meta, fp, account,
                t_start, ns_map, base_body=chat_req, session_key=session_key,
                realm=req_realm)


    def _responses_compaction_response(self, upstream_resp, model, fp, account,
                                       t_start, want_stream):
        """Answer Codex remote compaction v2 with exactly one `compaction` item.
        The upstream has no such concept, so the gateway asks for the summary."""
        try:
            chat_obj = translate.aggregate_stream(
                translate.iter_sse_lines(upstream_resp), model, None)
        except Exception as exc:
            record_row({"model": model, "outcome": "error", "status": 502,
                        "error": ("compaction failed: %s" % exc)[:200],
                        "elapsed_ms": int((time.time() - t_start) * 1000),
                        "account": account.uid, "fp": fp})
            return self._error(502, "upstream stream error: %s" % exc)
        choice = (chat_obj.get("choices") or [{}])[0]
        summary = str((choice.get("message") or {}).get("content") or "").strip()
        if not summary:
            record_row({"model": model, "outcome": "failed", "status": 502,
                        "error": "compaction produced no summary",
                        "elapsed_ms": int((time.time() - t_start) * 1000),
                        "account": account.uid, "fp": fp,
                        **_usage_fields(chat_obj.get("usage"))})
            return self._error(
                502, "compaction produced no summary (the model returned no text); retry")
        wall = int((time.time() - t_start) * 1000)
        item = {
            "id": translate._new_id("cmp_"),
            "type": "compaction",
            "encrypted_content": translate.encode_compaction_summary(summary),
        }
        usage = translate._responses_usage(chat_obj.get("usage")) or {}
        record_row({"model": model, "outcome": "ok", "status": 200,
                    "stream": want_stream, "elapsed_ms": wall,
                    "account": account.uid, "fp": fp,
                    **_usage_fields(chat_obj.get("usage"))})
        base = {
            "id": translate._new_id("resp_"),
            "object": "response",
            "created_at": int(time.time()),
            "model": model,
            "output": [item],
            "output_text": "",
            "metadata": {},
        }
        if not want_stream:
            return self._json(200, dict(base, status="completed", usage=usage))
        self._start_sse()
        seq = 0

        def ev(etype, payload):
            nonlocal seq
            seq += 1
            data = {"type": etype, "sequence_number": seq}
            data.update(payload)
            return ("event: " + etype + "\ndata: "
                    + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")

        try:
            empty = dict(base, status="in_progress", output=[])
            self.wfile.write(ev("response.created", {"response": empty}))
            self.wfile.write(ev("response.in_progress", {"response": empty}))
            # Codex's collector reads output_item.done only; an `added` frame
            # here would hand it an empty summary.
            self.wfile.write(ev("response.output_item.done",
                                {"output_index": 0, "item": item}))
            self.wfile.write(ev("response.completed", {
                "response": dict(base, status="completed",
                                 usage=usage or {"input_tokens": 0, "output_tokens": 0}),
            }))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True


    def open_upstream(self, payload, session_key, prebuilt=None, pin_account=None):
        # None when the caller did not pin a realm: upstream routes by model
        # (exclusive models to their exit, shared ones to the global realm).
        kwargs = {}
        if pin_account is not None:
            kwargs["pin_account"] = pin_account
        return upstream.open_upstream(_state.POOL, payload, session_key=session_key,
                                      target_realm=getattr(self, "_pinned_realm", None),
                                      log=log, prebuilt_body=prebuilt, **kwargs)


    def _upstream_error(self, model, exc, t_start, fp):
        wall = int((time.time() - t_start) * 1000)
        account_uid = getattr(exc, "account_uid", None)
        if isinstance(exc, upstream.ContentRejected):
            record_row({"model": model, "outcome": "error", "status": 403,
                        "error": exc.detail[:200], "elapsed_ms": wall,
                        "account": account_uid})
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        if isinstance(exc, upstream.RateLimited):
            record_row({"model": model, "outcome": "error", "status": 429,
                        "error": exc.detail[:200], "elapsed_ms": wall,
                        "account": account_uid})
            return self._rate_limited(exc)
        if isinstance(exc, urllib.error.HTTPError):
            detail = retry.read_http_error(exc, 600).decode("utf-8", "replace")
            record_row({"model": model, "outcome": "error", "status": exc.code,
                        "error": detail[:200], "elapsed_ms": wall, "account": account_uid})
            return self._error(exc.code, "upstream %s: %s" % (exc.code, detail))
        if isinstance(exc, upstream.TLSVerifyError):
            # The hint names the file/variable to fix; no retry will help.
            record_row({"model": model, "outcome": "error", "status": 502,
                        "error": str(exc)[:200], "elapsed_ms": wall,
                        "account": account_uid})
            return self._error(502, str(exc))
        if isinstance(exc, upstream.LocalNetworkError):
            record_row({"model": model, "outcome": "error", "status": 503,
                        "error": str(exc)[:200], "elapsed_ms": wall,
                        "account": account_uid})
            return self._error(503, str(exc), gateway_hint=(
                "local network unavailable: accounts were not penalised; "
                "check this host's DNS/route/proxy"))
        message = str(exc)
        record_row({"model": model, "outcome": "error", "status": 502,
                    "error": message[:200], "elapsed_ms": wall, "account": account_uid})
        if message.startswith("no usable account"):
            return self._error(503, message + " - add one with: python3 -m gateway.wbctl login")
        return self._error(502, "upstream unreachable: %s" % message)


    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()


    def _stream_chat(self, upstream_resp, model, fp, account, t_start,
                     reopen=None, headers_sent=False):
        if not headers_sent:
            self._start_sse()
        emitted = False
        last_usage = None
        first_ms = None
        streamed_text = []
        stream_id = None
        heartbeat = translate.SSEHeartbeat(
            lambda data: (self.wfile.write(data), self.wfile.flush()))
        try:
            with heartbeat:
                for line in translate.iter_sse_lines(upstream_resp):
                    data = translate.strip_data_prefix(line.decode("utf-8", "replace"))
                    if not data or data == "[DONE]" or data.startswith(":"):
                        continue
                    maybe = None
                    try:
                        maybe = json.loads(data)
                    except Exception:
                        pass
                    if isinstance(maybe, dict):
                        changed = False
                        u = maybe.get("usage")
                        if u:
                            if (last_usage is None or (u.get("total_tokens") or 0)
                                    >= (last_usage.get("total_tokens") or 0)):
                                last_usage = u
                            # usage belongs in the trailing choices-less frame
                            maybe.pop("usage", None)
                            changed = True
                        # one reply, one id: pin the first id seen for every frame
                        cid = maybe.get("id")
                        if cid:
                            if stream_id is None:
                                stream_id = cid
                            elif cid != stream_id:
                                maybe["id"] = stream_id
                                changed = True
                        for ch in (maybe.get("choices") or []):
                            delta = ch.get("delta") or {}
                            if delta.get("content"):
                                streamed_text.append(delta["content"])
                            if delta.get("reasoning_content"):
                                streamed_text.append(delta["reasoning_content"])
                        if changed:
                            data = json.dumps(maybe, ensure_ascii=False)
                    cleaned = translate.clean_chunk(data)
                    if not cleaned:
                        continue
                    if model:
                        # OpenAI echoes request.model; with an exit prefix the
                        # upstream chunks carry the bare name, so rewrite it back
                        try:
                            cobj = json.loads(cleaned)
                            if cobj.get("model") != model:
                                cobj["model"] = model
                                cleaned = json.dumps(cobj, ensure_ascii=False)
                        except Exception:
                            pass
                    if first_ms is None:
                        first_ms = int((time.time() - t_start) * 1000)
                    emitted = True
                    heartbeat.write(("data: %s\n\n" % cleaned).encode("utf-8"))
                if not emitted:
                    heartbeat.write(("data: %s\n\n" % json.dumps({
                        "error": {"message": "empty upstream stream",
                                  "type": "server_error"}})).encode("utf-8"))
                if last_usage is not None:
                    heartbeat.write(("data: %s\n\n" % translate.chat_usage_chunk(
                        model, last_usage, stream_id)).encode("utf-8"))
                heartbeat.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            record_row({"model": model, "outcome": "client_aborted", "status": 200,
                        "stream": True, "elapsed_ms": wall, "ttft_ms": first_ms,
                        "account": account.uid, "fp": fp, **_usage_fields(last_usage)})
            return
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            log("stream aborted: %s" % exc, "WARN")
            if not emitted and reopen is not None:
                # Nothing reached the client yet: reopen on the same account
                # once, reusing the headers already on the wire.
                try:
                    upstream_resp.close()
                except Exception:
                    pass
                new_resp = None
                try:
                    new_resp, new_account = reopen()
                except Exception as reopen_exc:
                    log("stream reopen failed: %s" % reopen_exc, "WARN")
                if new_resp is not None:
                    try:
                        return self._stream_chat(new_resp, model, fp, new_account,
                                                 t_start, reopen=None,
                                                 headers_sent=True)
                    finally:
                        try:
                            new_resp.close()
                        except Exception:
                            pass
            record_row({"model": model, "outcome": "upstream_aborted", "status": 502,
                        "error": str(exc)[:200], "stream": True, "elapsed_ms": wall,
                        "ttft_ms": first_ms, "account": account.uid, "fp": fp,
                        **_usage_fields(last_usage)})
            # No [DONE]: the client must see the truncation, not a clean end.
            return
        wall = int((time.time() - t_start) * 1000)
        if last_usage is None or (last_usage.get("total_tokens") or 0) == 0:
            comp = translate.estimate_tokens("".join(streamed_text))
            if comp:
                last_usage = {"prompt_tokens": max(1, comp // 2),
                              "completion_tokens": comp,
                              "total_tokens": max(1, comp // 2) + comp}
        record_row({"model": model, "outcome": "ok", "status": 200, "stream": True,
                    "elapsed_ms": wall, "ttft_ms": first_ms, "gen_ms":
                    (wall - first_ms) if first_ms is not None else None,
                    "account": account.uid, "fp": fp, **_usage_fields(last_usage)})


    def _nonstream_chat(self, upstream_resp, model, fp, account, t_start):
        try:
            result = translate.aggregate_stream(
                translate.iter_sse_lines(upstream_resp), model, None)
        except Exception as exc:
            record_row({"model": model, "outcome": "error", "status": 502,
                        "error": str(exc)[:200],
                        "elapsed_ms": int((time.time() - t_start) * 1000),
                        "account": account.uid, "fp": fp})
            return self._error(502, "upstream stream error: %s" % exc)
        wall = int((time.time() - t_start) * 1000)
        first_at = result.pop("first_chunk_at", None)
        result.pop("elapsed_ms", None)
        result["model"] = model   # echo the requested name, prefix included
        record_row({"model": model, "outcome": "ok", "status": 200, "stream": False,
                    "elapsed_ms": wall,
                    "ttft_ms": int((first_at - t_start) * 1000) if first_at else None,
                    "account": account.uid, "fp": fp, **_usage_fields(result.get("usage"))})
        return self._json(200, result)


    def _stream_responses(self, upstream_resp, model, custom_names, request_meta,
                          fp, account, t_start, ns_map, base_body=None,
                          session_key=None, realm=None):
        self._start_sse()
        holder = {"usage": None, "custom_names": custom_names,
                  "request_meta": request_meta, "namespace_map": ns_map,
                  "base_body": base_body,
                  "base_messages": (base_body or {}).get("messages"),
                  "session_key": session_key, "realm": realm}
        first_ms = None
        upstream = upstream_resp
        heartbeat = translate.SSEHeartbeat(
            lambda data: (self.wfile.write(data), self.wfile.flush()))
        try:
            with heartbeat:
                # When the model asks for web_search / web_fetch, the gateway
                # runs it and feeds the result back; the client sees one stream.
                rounds = 0
                total_usage = None
                while True:
                    holder.pop("internal_calls", None)
                    holder.pop("suppress_completion", None)
                    holder["suppress_lifecycle"] = rounds > 0
                    for frame in translate.stream_responses_events(upstream, model, holder):
                        if first_ms is None:
                            first_ms = int((time.time() - t_start) * 1000)
                        heartbeat.write(translate.clean_responses_frame(frame))
                    # Every round really spends tokens; accumulate for accounting.
                    total_usage = translate.sum_usage(total_usage, holder.get("usage"))
                    internal = holder.get("internal_calls") or []
                    if not internal:
                        break
                    rounds += 1
                    # Retract the tools and let the model close with text.
                    give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    upstream, account = translate.follow_up_with_tool_results(
                        _state.POOL, internal, holder, model, session_key, t_start,
                        drop_tools=give_up)
                if total_usage:
                    holder["usage"] = total_usage
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            record_row({"model": model, "outcome": "client_aborted", "status": 200,
                        "stream": True, "elapsed_ms": int((time.time() - t_start) * 1000),
                        "ttft_ms": first_ms, "account": account.uid, "fp": fp,
                        **_usage_fields(holder.get("usage"))})
            return
        except Exception as exc:
            log("responses stream aborted: %s" % exc, "WARN")
            record_row({"model": model, "outcome": "upstream_aborted", "status": 502,
                        "error": str(exc)[:200], "stream": True,
                        "elapsed_ms": int((time.time() - t_start) * 1000),
                        "ttft_ms": first_ms, "account": account.uid, "fp": fp,
                        **_usage_fields(holder.get("usage"))})
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        finally:
        # Gateway rounds replace the upstream, so the outer with only knows
        # the first one; close the last one here.
            try:
                upstream.close()
            except Exception:
                pass
        record_row({"model": model,
                    "outcome": "failed" if holder.get("empty_reasoning") else "ok",
                    "status": 200, "stream": True,
                    "elapsed_ms": int((time.time() - t_start) * 1000),
                    "ttft_ms": first_ms, "account": account.uid, "fp": fp,
                    **_usage_fields(holder.get("usage"))})


    def _nonstream_responses(self, upstream_resp, model, custom_names, request_meta,
                             fp, account, t_start, ns_map, base_body=None,
                             session_key=None, realm=None):
        # As when streaming: gateway-run web calls stay invisible until the
        # final Responses object, never as a raw function_call.
        sources = []
        rounds = 0
        upstream = upstream_resp
        # With the switch off, do not intercept same-named calls; those are the client's.
        web_tools = translate.web_tools_active(base_body)
        while True:
            try:
                chat_obj = translate.aggregate_stream(
                    translate.iter_sse_lines(upstream), model, None)
            except Exception as exc:
                record_row({"model": model, "outcome": "error", "status": 502,
                            "error": str(exc)[:200],
                            "elapsed_ms": int((time.time() - t_start) * 1000),
                            "account": account.uid, "fp": fp})
                return self._error(502, "upstream stream error: %s" % exc)
            calls = translate.internal_calls_from_chat(chat_obj, web_tools=web_tools)
            if not calls:
                break
            rounds += 1
            give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
            try:
                upstream.close()
            except Exception:
                pass
            holder = {"base_messages": (base_body or {}).get("messages"),
                      "base_body": base_body, "realm": realm,
                      "web_sources": sources}
            try:
                upstream, account = translate.follow_up_with_tool_results(
                    _state.POOL, calls, holder, model, session_key, t_start,
                    drop_tools=give_up)
            except Exception as exc:
                record_row({"model": model, "outcome": "error", "status": 502,
                            "error": "web tool follow-up failed: %s" % str(exc)[:200],
                            "elapsed_ms": int((time.time() - t_start) * 1000),
                            "account": account.uid, "fp": fp})
                return self._error(502, "web tool follow-up failed: %s" % exc)
            sources = holder.get("web_sources") or sources
        wall = int((time.time() - t_start) * 1000)
        chat_obj.pop("first_chunk_at", None)
        chat_obj.pop("elapsed_ms", None)
        chat_obj["model"] = model   # echo the requested name, prefix included
        result = translate.chat_to_response(chat_obj, model, custom_names,
                                            request_meta, ns_map, sources=sources)
        if translate.reasoning_only_response(result):
            record_row({"model": model, "outcome": "empty_reasoning", "status": 502,
                        "error": "upstream returned reasoning without output text",
                        "elapsed_ms": wall, "account": account.uid, "fp": fp,
                        **_usage_fields(chat_obj.get("usage"))})
            return self._error(
                502, "upstream returned reasoning without output text; retry")
        record_row({"model": model, "outcome": "ok", "status": 200, "stream": False,
                    "elapsed_ms": wall, "account": account.uid, "fp": fp,
                    **_usage_fields(chat_obj.get("usage"))})
        return self._json(200, result)
