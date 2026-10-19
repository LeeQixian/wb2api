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
            self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
            return {"ok": (code == 0 or code == 10001), "code": code, "msg": msg,
                    "data": payload.get("data")}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:
                body = {}
            code = body.get("code")
            msg = body.get("msg") or ("HTTP %d" % exc.code)
            # upstream answers "already checked in today" as HTTP 400/10001
            if code == 10001 or "已签到" in msg:
                self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
                return {"ok": True, "code": code, "msg": msg, "data": body.get("data")}
            return {"ok": False, "error": msg}
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
            # These two ride every web-console session creation (captured); keep them.
            "conversationOrigin": "workbuddy-app",
            "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
        }
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
            return {"ok": False, "error": "HTTP %d: %s" % (exc.code, detail[:120])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "unexpected response"}
        conversation = (payload.get("data") or {}).get("id")
        if payload.get("code") not in (0, None) or not conversation:
            return {"ok": False, "error": "code=%s msg=%s"
                    % (payload.get("code"), payload.get("msg"))}

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
            result["msg"] = "网页通道会话跑完：%d 段输出，%d ms" % (
                result.get("chunks") or 0, result.get("elapsed_ms") or 0)
        return result


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
