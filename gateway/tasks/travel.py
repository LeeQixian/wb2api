"""Cat travel: status, depart and the missing-Buddy self-heal."""

import json
import time
import urllib.error
import urllib.request

from gateway import accounts as _accounts
from gateway.common.log import log
from gateway.tasks.base import CHAT_BASE, WEB_BASE
from gateway.tasks.growth import _progress_one, fetch_growth_tasks


def _travel_status(account):
    req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status",
                                 headers=account.headers("chat"))
    with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
        d = json.loads(resp.read().decode("utf-8"))
        return d.get("data") or {}


def ensure_buddy(account, gap=1.0):
    """Complete and claim first_buddy (its reward is the Buddy instance)."""
    tasks = fetch_growth_tasks(account)
    fb = next((t for t in tasks if t["task_code"] == "first_buddy"), None)
    if fb is None:
        return False
    if fb["status"] == "claimed":
        return True
    _progress_one(account, fb, [], gap)
    tasks = fetch_growth_tasks(account)
    fb = next((t for t in tasks if t["task_code"] == "first_buddy"), None)
    return bool(fb and fb["status"] == "claimed")


def _travel_depart(account, lid):
    """Depart a trip; returns (data, err)."""
    body = json.dumps({"location_id": lid}).encode("utf-8")
    req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/depart",
                                 data=body, method="POST", headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") == 0:
            return res.get("data") or {}, None
        return None, res.get("msg") or ("code=%s" % res.get("code"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        try:
            msg = json.loads(detail).get("msg") or ("HTTP %s" % exc.code)
        except Exception:
            msg = "HTTP %s%s" % (exc.code, (": " + detail[:120]) if detail else "")
        return None, msg
    except Exception as exc:
        return None, str(exc)


def do_cat_travel(account):
    """Check and run cat travel: claim a returned trip or depart a new one."""
    if getattr(account, "is_enterprise", lambda: False)():
        return {"ok": True, "action": "skip", "msg": "企业版账号无成长体系，跳过"}
    headers = account.headers("chat")
    # Query the current travel state.
    try:
        st = _travel_status(account)
    except Exception as exc:
        return {"ok": False, "msg": f"查询旅行状态失败: {exc}"}

    state = st.get("state")
    if state == "arrived":
        # Claim (the official frontend POSTs travel/claim with an empty body).
        req_cl = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/claim", data=b"{}", method="POST", headers=headers)
        try:
            with _accounts.urlopen(req_cl, timeout=10, proxy=account.proxy) as resp:
                c_res = json.loads(resp.read().decode("utf-8"))
                credit = (c_res.get("data") or {}).get("reward_credit", 0)
                account.fetch_credits()
                return {"ok": True, "action": "claim", "credit": credit, "msg": f"旅行归来领奖成功！获得 {credit} 积分"}
        except Exception as e:
            return {"ok": False, "msg": f"领奖失败: {e}"}

    if state == "idle":
        if st.get("daily_limit_reached"):
            return {"ok": True, "action": "idle", "msg": "猫猫今日已完成旅行，明日 00:00 刷新"}
        # Departure needs a {location_id} body (destinations from travel/config);
        # an empty body is rejected with 400 "invalid request".
        lid = None
        try:
            req_cfg = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/config", headers=headers)
            with _accounts.urlopen(req_cfg, timeout=10, proxy=account.proxy) as resp:
                cfg = json.loads(resp.read().decode("utf-8"))
                locs = (cfg.get("data") or {}).get("locations") or []
            if locs:
                lid = locs[0].get("id")
        except Exception as exc:
            _log(f"buddy/travel/config query failed: {exc}")
        if lid is None:
            lid = 1
        # status buddy_id is the current trip instance (0 at home), so it
        # proves nothing; repair once anyway.
        data, err = _travel_depart(account, lid)
        if data is None and "no active buddy" in (err or "").lower():
            if ensure_buddy(account):
                data, err = _travel_depart(account, lid)
            else:
                return {"ok": False,
                        "msg": "还没有 Buddy：需先完成并领取「领取一只 Buddy」任务（新建任务并发起对话）"}
        if data is not None:
            loc = (data.get("location") or {}).get("name") or ""
            return {"ok": True, "action": "depart", "msg": f"猫猫已出发前往「{loc}」，预计数小时后归来！"}
        return {"ok": False, "msg": f"派出旅行失败: {err}"}

    if state == "traveling":
        return {"ok": True, "action": "traveling", "msg": "猫猫正在旅行途中，请稍后再来查看！"}

    return {"ok": True, "action": state, "msg": f"当前状态: {state}"}
