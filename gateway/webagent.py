# -*- coding: utf-8 -*-
"""Drive one web-console (cloud agent) conversation via ACP over HTTP+SSE.

POST /console/as/conversations/ only *queues* a conversation; the agent runs
once a client attaches to that conversation's sandbox (the `link` from
GET .../{id}/session) and requests the turn. Sequence: GET <link> with
Accept: text/event-stream (returns an SSE stream and an Acp-Connection-Id
header), then POST <link> with JSON-RPC initialize -> session/load ->
session/prompt, while session/update notifications stream back.

Only the parts the daily check-in needs are implemented: no tools, no
terminal, no filesystem callbacks.
"""

import base64
import http.client
import json
import queue
import threading
import time
import urllib.parse

PROTOCOL_VERSION = 1

#: The check-in needs only the agent to finish; no fs/terminal access.
CLIENT_CAPABILITIES = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}


def _proxy_parts(proxy_url):
    """Parse proxy URL -> (host, port, CONNECT headers), like ProxyHandler."""
    raw = str(proxy_url or "").strip()
    parsed = urllib.parse.urlparse(raw)
    host, port = parsed.hostname, parsed.port
    user, password = parsed.username, parsed.password
    if not host:
        # scheme-less "user:pass@host:port" reads as a scheme to urlparse
        authority = raw.split("://")[-1].split("/")[0]
        userinfo, _, hostport = authority.rpartition("@")
        host, _, port_text = hostport.rpartition(":")
        if not host:
            host, port_text = hostport, ""
        try:
            port = int(port_text) if port_text else None
        except ValueError:
            port = None
        if userinfo:
            user, _, password = userinfo.partition(":")
    headers = {}
    if user and password:
        creds = "%s:%s" % (urllib.parse.unquote(user),
                           urllib.parse.unquote(password))
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
            creds.encode("utf-8")).decode("ascii")
    return host, port, headers


class AcpError(Exception):
    """Raised on a drive failure; the caller turns it into readable text."""


class AcpChannel(object):
    """ACP streamable-HTTP transport: one SSE stream in, POST requests out."""

    def __init__(self, link, token, user_agent, log=None, proxy="", timeout=30):
        self.link = link
        self.token = token
        self.user_agent = user_agent
        self.log = log or (lambda *a, **k: None)
        self.proxy = proxy or ""
        self.timeout = timeout
        self.connection_id = ""
        self.events = queue.Queue()
        self.updates = 0
        self.chunks = 0
        self._conn = None
        self._reader = None
        self._stop = threading.Event()

    # ---- connection -----------------------------------------------
    def _target(self):
        parsed = urllib.parse.urlparse(self.link)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AcpError("沙箱地址不可用: %r" % self.link)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed, port

    def _connect(self):
        parsed, port = self._target()
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = cls(parsed.hostname, port, timeout=self.timeout)
        if self.proxy:
            # set_tunnel does not add Proxy-Authorization on its own (hub a94eb84)
            host, proxy_port, tunnel_headers = _proxy_parts(self.proxy)
            conn = cls(host, proxy_port or 8080, timeout=self.timeout)
            conn.set_tunnel(parsed.hostname, port, headers=tunnel_headers)
        return conn

    def open(self):
        """Open the SSE channel; return the server's Acp-Connection-Id."""
        parsed, _port = self._target()
        conn = self._connect()
        conn.putrequest("GET", parsed.path or "/")
        conn.putheader("Accept", "text/event-stream")
        conn.putheader("Authorization", "Bearer " + self.token)
        conn.putheader("User-Agent", self.user_agent)
        conn.endheaders()
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            raise AcpError("SSE 通道返回 HTTP %s" % resp.status)
        connection_id = resp.getheader("Acp-Connection-Id") or ""
        if not connection_id:
            conn.close()
            raise AcpError("SSE 通道没有返回 Acp-Connection-Id")
        self._conn = conn
        self.connection_id = connection_id
        self._reader = threading.Thread(target=self._read_events, args=(resp,), daemon=True)
        self._reader.start()
        return connection_id

    def _read_events(self, resp):
        """Parse the SSE stream into JSON-RPC messages on self.events."""
        try:
            while not self._stop.is_set():
                line = resp.fp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "session/update":
                    update = (message.get("params") or {}).get("update") or {}
                    self.updates += 1
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        self.chunks += 1
                self.events.put(message)
        except Exception as exc:      # connection closed, timeout, etc.
            self.events.put({"__stream_error__": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            self.events.put(None)     # end-of-stream sentinel

    def request(self, method, params, request_id):
        """Send one JSON-RPC request; returns the HTTP status (normally 202)."""
        parsed, _port = self._target()
        body = json.dumps({"jsonrpc": "2.0", "id": request_id,
                           "method": method, "params": params}).encode("utf-8")
        conn = self._connect()
        try:
            conn.putrequest("POST", parsed.path or "/")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Accept", "application/json, text/event-stream")
            conn.putheader("Acp-Connection-Id", self.connection_id)
            conn.putheader("Authorization", "Bearer " + self.token)
            conn.putheader("User-Agent", self.user_agent)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            conn.send(body)
            resp = conn.getresponse()
            resp.read()
            status = resp.status
        finally:
            conn.close()
        if status not in (200, 202):
            raise AcpError("%s 返回 HTTP %s" % (method, status))
        return status

    def close(self):
        self._stop.set()
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _set_model(channel, session_id, model, log):
    # Manifest model is decorative; only this ACP call picks the running one.
    try:
        channel.request("session/set_model",
                        {"sessionId": session_id, "modelId": model}, 3)
    except Exception as exc:
        log("web agent: 设置模型 %s 失败 (%s)，本轮按会话默认模型跑" % (model, exc))


def run_turn(link, token, session_id, cwd, prompt, user_agent, poll_status=None,
             wait_seconds=120, poll_interval=3, log=None, proxy="",
             channel_factory=AcpChannel, model=None):
    """Attach to the sandbox and drive the turn to completion.

    poll_status is the caller's conversation-state lookup (console API):
    completed finishes, failed/error stops. The result carries event and delta
    counts so the check-in log shows whether the turn actually ran. Non-empty
    model: pin it via session/set_model after load (hub de4fb53).
    """
    log = log or (lambda *a, **k: None)
    started = time.time()
    channel = channel_factory(link, token, user_agent, log=log, proxy=proxy)
    status = ""
    error = ""
    finished = False
    try:
        channel.open()
        channel.request("initialize",
                        {"protocolVersion": PROTOCOL_VERSION,
                         "clientCapabilities": CLIENT_CAPABILITIES}, 1)
        channel.request("session/load",
                        {"sessionId": session_id, "cwd": cwd or "/workspace",
                         "mcpServers": []}, 2)
        if model:
            _set_model(channel, session_id, model, log)
        channel.request("session/prompt",
                        {"sessionId": session_id,
                         "prompt": [{"type": "text", "text": prompt}]}, 4)
        deadline = time.time() + max(1, int(wait_seconds))
        while time.time() < deadline:
            if poll_status is not None:
                try:
                    status = str(poll_status() or "")
                except Exception as exc:
                    log("web agent: 状态查询失败 (%s)" % exc)
                if status == "completed":
                    finished = True
                    break
                if status in ("failed", "error"):
                    error = "会话状态=%s" % status
                    break
            if poll_interval:
                time.sleep(poll_interval)
    except AcpError as exc:
        error = str(exc)
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        channel.close()
    elapsed_ms = int((time.time() - started) * 1000)
    if not status and poll_status is not None:
        try:
            status = str(poll_status() or "")
        except Exception:
            pass
    if not finished and not error:
        error = "会话在 %ss 内没有跑完（状态=%s）" % (wait_seconds, status or "未知")
    ok = finished and not error
    return {"ok": ok, "status": status, "events": channel.updates,
            "chunks": channel.chunks, "elapsed_ms": elapsed_ms, "error": error}
