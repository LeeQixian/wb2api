"""Account maintenance: check-in, daily activity and the web-channel session."""

import json
import os
import time
import urllib.error
import urllib.request

from gateway import settings as wb_settings
from gateway import webagent as wb_webagent
from gateway.account.credentials import fetch_account_profile
from gateway.account import transport as _transport
from gateway.account.jwt import (CHECKIN_PATH, _jwt_claims, get_realm_config)
from gateway.account import transport as _transport
from gateway.account.web_config import (DAILY_CHAT_MODEL, DAILY_CHAT_WEB_PROMPT,
                                         PROFILE_PATH, WEB_CONVERSATIONS_URL,
                                         WEB_ORIGIN, WEB_ORIGIN_CN,
                                         WEB_TURN_TIMEOUT, WEB_USER_AGENT)
from gateway.common.log import log

# Upstream allows 10 active (status=working) conversations per account; a session
# that is never stopped leaks a slot until creation fails with 429 / code 10105.
WEB_CONVERSATION_ACTIVE_LIMIT = 10
WEB_CONVERSATION_PRUNE_KEEP = 8
WEB_CONVERSATION_ACTIVE_STATUSES = ("working", "creating", "pending")

# A turn thread can wedge in socket land, so its finally never runs and the session
# holds one of the 10 upstream slots; this in-process ledger lets a sweeper close it.
WEB_SESSION_REGISTRY_MAX = 128
# Grace beyond the turn budget before a registered session counts as abandoned.
WEB_SESSION_RECLAIM_GRACE = 300


def _active_conversation_limit(status, detail):
    """True when a rejection means the account has no free active-conversation slot."""
    if status != 429:
        return False
    text = str(detail or "")
    return ("10105" in text
            or "maximum number of active conversations" in text.lower())


CHECKIN_PENDING_HINTS = ("处理中", "请勿重复操作", "系统繁忙", "稍后再试")
CHECKIN_ALREADY_HINTS = ("已签到",)
CHECKIN_FAILURE_HINTS = ("失败",)


def checkin_state(code, msg):
    """Classify a check-in receipt: checked / already / pending / failed."""
    text = str(msg or "")
    if any(k in text for k in CHECKIN_PENDING_HINTS):
        return "pending"
    if code == 0:
        # The insufficient-balance wording also appears in success text, so only
        # the failure wording counts as a hard failure.
        return "failed" if any(k in text for k in CHECKIN_FAILURE_HINTS) else "checked"
    if code == 10001:
        return "already" if any(k in text for k in CHECKIN_ALREADY_HINTS) else "failed"
    return "failed"


class MaintenanceMixin(object):
    """Mixed into Account; relies on headers()/save()/chat_base_url()/fetch_credits()."""
    def is_enterprise(self):
        """Enterprise accounts have no personal growth/checkin system."""
        return bool(self.enterprise_id or self.jwt_enterprise_id())


    def jwt_enterprise_id(self, token=None):
        """Enterprise space id from the token itself (file field is often empty)."""
        return str(_jwt_claims(token or self.access_token).get("enterprise_id") or "")


    def can_checkin(self):
        if self.realm != "cn":
            return False
        if self.is_enterprise():
            return False
        if not self.last_checkin:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_checkin).startswith(today_str)


    def checkin(self):
        if self.realm != "cn":
            return {"ok": False, "error": "checkin is only available for CN realm accounts"}
        if self.is_enterprise():
            return {"ok": False, "error": "enterprise accounts do not support checkin"}
        cfg = get_realm_config("cn")
        url = cfg["billing_upstream"] + CHECKIN_PATH
        headers = self.headers(purpose="billing")
        try:
            payload = _transport.http_json(url, data=b"{}", method="POST", headers=headers, timeout=15,
                                proxy=self.proxy)
            code = payload.get("code", -1)
            msg = payload.get("msg") or "ok"
            state = checkin_state(code, msg)
            if state in ("checked", "already"):
                self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
                return {"ok": True, "state": state, "code": code, "msg": msg,
                        "data": payload.get("data")}
            return {"ok": False, "state": state, "code": code, "msg": msg, "error": msg}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:
                body = {}
            code = body.get("code")
            msg = body.get("msg") or ("HTTP %d" % exc.code)
            # upstream answers "already checked in today" as HTTP 400/10001
            state = checkin_state(code, msg)
            if state == "already":
                self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
                return {"ok": True, "state": state, "code": code, "msg": msg,
                        "data": body.get("data")}
            return {"ok": False, "state": state, "code": code, "msg": msg, "error": msg}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


    def can_daily_chat(self):
        if self.realm != "intl":
            return False
        if not self.last_daily_chat:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_daily_chat).startswith(today_str)


    def web_headers(self):
        """Web-app outbound headers: bearer plus X-User-Id, no desktop fingerprint."""
        return {
            "Authorization": "Bearer " + self.access_token,
            "X-User-Id": self.uid,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/app",
            "User-Agent": WEB_USER_AGENT,
        }


    def daily_chat_web(self, prompt=None):
        """Daily-active web session.
        Creating it is not enough: the agent runs once this client prompts it."""
        if self.realm != "intl":
            return {"ok": False, "error": "web daily chat is only for international accounts"}
        body = {
            "prompt": prompt or DAILY_CHAT_WEB_PROMPT,
            "model": DAILY_CHAT_MODEL,
            # Both ride every web-console session creation; keep them.
            "conversationOrigin": "workbuddy-app",
            "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
        }
        conversation, failure = self._web_conversation_create(body)
        if failure and failure.get("limit"):
            # A leaked-slot limit: stop the oldest gateway sessions and create once
            # more; if nothing frees up, do not retry again.
            freed = self.web_conversation_prune()
            if freed:
                conversation, failure = self._web_conversation_create(body)
            if failure:
                hint = ("已自动结束 %d 条旧会话" % freed) if freed else "没有可自动结束的旧会话"
                return {"ok": False,
                        "error": "活跃会话已达上限（%d）：%s；请到 %s 网页端结束或删除旧对话后重试"
                                 % (WEB_CONVERSATION_ACTIVE_LIMIT, hint,
                                    WEB_ORIGIN.replace("https://", "")),
                        "retryable": False}
        if failure:
            return {"ok": False, "error": failure.get("message") or "建会话失败",
                    "retryable": bool(failure.get("retryable"))}

        # Ledger it before the first network hop: the slot is live upstream from
        # here on, and only this thread's finally or the reclaimer gives it back.
        self.web_session_register(conversation)
        keep = False
        try:
            # Creating only queues; attach the sandbox and request the turn to run it.
            try:
                session = (self._web_conversation_get(conversation, "/session") or {}).get("data") or {}
            except Exception as exc:
                return {"ok": False, "conversation": conversation,
                        "error": "session 查询失败: %s" % exc}
            link = session.get("link") or session.get("endpoint") or ""
            token = session.get("token") or ""
            session_id = session.get("sessionId") or session.get("session_id") or conversation
            cwd = session.get("cwd") or "/workspace"
            if not link or not token:
                return {"ok": False, "conversation": conversation,
                        "error": "沙箱未就绪（没有 link/token）"}

            result = wb_webagent.run_turn(
                link, token, session_id, cwd, prompt or DAILY_CHAT_WEB_PROMPT,
                WEB_USER_AGENT,
                poll_status=lambda: self._web_conversation_status(conversation),
                wait_seconds=WEB_TURN_TIMEOUT, proxy=self.proxy,
                model=DAILY_CHAT_MODEL)
            result["conversation"] = conversation
            if result.get("ok"):
                keep = True
                result["msg"] = "网页通道会话跑完：%d 段输出，%d ms" % (
                    result.get("chunks") or 0, result.get("elapsed_ms") or 0)
            return result
        finally:
            # Always release: a live session holds a slot. Success stops it and keeps
            # history, failure deletes it; a failed release stays for the reclaimer.
            self.web_session_mark(conversation, delete=not keep)
            if self.web_conversation_release(conversation, delete=not keep):
                self.web_session_release(conversation)


    def _web_conversation_get(self, conversation, suffix=""):
        """Read one web session ("" = the session, "/session" = sandbox info)."""
        url = WEB_CONVERSATIONS_URL + urllib.parse.quote(str(conversation)) + suffix
        req = urllib.request.Request(url, headers=self.web_headers(), method="GET")
        with _transport.urlopen(req, timeout=30, proxy=self.proxy) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")


    def _web_conversation_status(self, conversation):
        """Current status of this session (completed = the turn really ran)."""
        try:
            payload = self._web_conversation_get(conversation)
        except Exception:
            return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        return str((data or {}).get("status") or "")


    def _web_conversation_call(self, conversation, action):
        """Run stop/delete on one session; cleanup never raises into the caller."""
        if not conversation:
            return False
        url = (WEB_CONVERSATIONS_URL + urllib.parse.quote(str(conversation))
               + "/" + action)
        req = urllib.request.Request(url, data=b"{}",
                                     headers=self.web_headers(), method="POST")
        try:
            with _transport.urlopen(req, timeout=20, proxy=self.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except Exception:
            return False
        return isinstance(payload, dict) and payload.get("code") in (0, None)


    def web_conversation_release(self, conversation, delete=False):
        """Stop a session to free its active slot; delete too when the run failed."""
        if not conversation:
            return False
        stopped = self._web_conversation_call(conversation, "stop")
        if delete:
            return self._web_conversation_call(conversation, "delete") or stopped
        return stopped


    def web_session_register(self, conversation, delete=True):
        """Ledger a live session so a wedged turn cannot leak the slot forever.
        `delete` is what a reclaim does: failures delete, finished runs only stop."""
        if not conversation:
            return
        with self._web_sessions_lock:
            if (conversation not in self._web_sessions
                    and len(self._web_sessions) >= WEB_SESSION_REGISTRY_MAX):
                oldest = min(self._web_sessions,
                             key=lambda key: self._web_sessions[key]["ts"])
                self._web_sessions.pop(oldest, None)
                log("account %s: web session registry full, dropped %s"
                    % (self.uid, oldest))
            self._web_sessions[conversation] = {"ts": time.time(),
                                                "delete": bool(delete)}


    def web_session_mark(self, conversation, delete):
        """Update what a reclaim should do once the turn's outcome is known."""
        with self._web_sessions_lock:
            entry = self._web_sessions.get(conversation)
            if entry is not None:
                entry["delete"] = bool(delete)


    def web_session_release(self, conversation):
        """Forget one registered session; True when it was there."""
        with self._web_sessions_lock:
            return self._web_sessions.pop(conversation, None) is not None


    def reclaim_web_sessions(self, max_age=None, now=None):
        """Close sessions whose turn thread never reached its finally block.
        A failed stop/delete stays registered so the next sweep retries it."""
        if max_age is None:
            max_age = WEB_TURN_TIMEOUT + WEB_SESSION_RECLAIM_GRACE
        now = time.time() if now is None else now
        with self._web_sessions_lock:
            stale = [(conversation, entry["delete"])
                     for conversation, entry in self._web_sessions.items()
                     if now - entry["ts"] >= max_age]
        reclaimed = 0
        for conversation, delete in stale:
            try:
                if self.web_conversation_release(conversation, delete=delete):
                    self.web_session_release(conversation)
                    reclaimed += 1
            except Exception as exc:
                log("account %s: web session reclaim failed for %s: %s"
                    % (self.uid, conversation, exc))
        return reclaimed


    def web_conversation_list(self, page_size=50):
        """List this account's web sessions (upstream returns newest first)."""
        url = WEB_CONVERSATIONS_URL + "?page=1&pageSize=%d" % max(1, int(page_size))
        req = urllib.request.Request(url, headers=self.web_headers(), method="GET")
        with _transport.urlopen(req, timeout=20, proxy=self.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        data = (payload if isinstance(payload, dict) else {}).get("data") or {}
        conversations = data.get("conversations")
        return conversations if isinstance(conversations, list) else []


    def web_conversation_prune(self, keep=WEB_CONVERSATION_PRUNE_KEEP):
        """Stop the oldest gateway-created active sessions; returns how many.
        Only sessions named after the daily-activity prompt are touched."""
        try:
            conversations = self.web_conversation_list()
        except Exception:
            return 0
        active = [c for c in conversations
                  if str((c or {}).get("status") or "").lower()
                  in WEB_CONVERSATION_ACTIVE_STATUSES]
        if len(active) < WEB_CONVERSATION_ACTIVE_LIMIT:
            return 0
        active.sort(key=lambda c: str((c or {}).get("createdAt") or ""))
        stopped = 0
        for conversation in active:
            if len(active) - stopped <= keep:
                break
            if str((conversation or {}).get("name") or "").strip() != DAILY_CHAT_WEB_PROMPT:
                continue
            if self._web_conversation_call(conversation.get("id"), "stop"):
                stopped += 1
        return stopped


    def _web_conversation_create(self, body):
        """Create one web session; returns (conversation id, failure dict or None)."""
        req = urllib.request.Request(
            WEB_CONVERSATIONS_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self.web_headers(), method="POST")
        try:
            with _transport.urlopen(req, timeout=30, proxy=self.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            return None, {"message": "HTTP %d: %s" % (exc.code, detail[:120]),
                          "retryable": exc.code >= 500 or exc.code in (408, 429),
                          "limit": _active_conversation_limit(exc.code, detail)}
        except Exception as exc:
            return None, {"message": str(exc), "retryable": True, "limit": False}
        if not isinstance(payload, dict):
            return None, {"message": "unexpected response", "retryable": True,
                          "limit": False}
        conversation = (payload.get("data") or {}).get("id")
        if payload.get("code") not in (0, None) or not conversation:
            return None, {"message": "code=%s msg=%s" % (payload.get("code"),
                                                         payload.get("msg")),
                          "retryable": False, "limit": False}
        return conversation, None


    def daily_chat(self, web=None):
        """Intl daily-active chat: desktop turn plus the web-channel session.
        web=None follows settings.json daily_chat_web; upstream is imported lazily."""
        if self.realm != "intl":
            return {"ok": False, "error": "daily chat is only for international accounts"}
        from gateway import upstream as wb_proxy
        url = self.chat_base_url() + wb_proxy.CHAT_PATH
        headers = self.headers("chat")
        body = {
            "model": DAILY_CHAT_MODEL,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
            "max_tokens": 10,
            "reasoning_effort": "none",
        }
        req_body = wb_proxy.build_upstream_body(body)
        # Same outbound wire shape as open_upstream: compact JSON, verbatim names.
        from gateway.upstream import retry as wb_retry
        data = wb_retry.encode_upstream_body(req_body)
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        req.wb_exact_headers = list(headers.items())
        try:
            with _transport.urlopen(req, timeout=20, proxy=self.proxy) as resp:
                _ = resp.read()
            if web is None:
                web = bool(self.path) and wb_settings.daily_chat_web(os.path.dirname(self.path))
            web_res = self.daily_chat_web() if web else None
            # Only a finished web session counts for the daily bonus, so only
            # it closes the day; a failed leg stays unset.
            if not web or (web_res or {}).get("ok"):
                self.last_daily_chat = time.strftime("%Y-%m-%d %H:%M:%S")
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
            try:
                self.fetch_credits()
            except Exception:
                pass
            result = {"ok": True, "msg": "每日活跃对话成功完成"}
            if web_res is not None:
                result["web"] = web_res
                if web_res.get("ok"):
                    result["msg"] = ("每日活跃对话成功完成（网页通道 %s：%d 段输出，%d ms）"
                                     % (web_res.get("status") or "completed",
                                        web_res.get("chunks") or 0,
                                        web_res.get("elapsed_ms") or 0))
                else:
                    # Only the web leg earns the bonus; a dead one is a failed day.
                    result["ok"] = False
                    result["error"] = "网页通道失败：%s" % web_res.get("error")
                    result["msg"] = ("每日活跃对话未完成（网页通道失败：%s）"
                                     % web_res.get("error"))
            return result
        except urllib.error.HTTPError as exc:
            try:
                err = exc.read().decode("utf-8", "replace")
            except Exception:
                err = str(exc)
            return {"ok": False, "error": f"HTTP {exc.code}: {err[:100]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
