"""Growth tasks: fetch, accept, claim and report (opt-in via growthplan)."""

import json
import os
import random
import time
import urllib.error
import urllib.request

from gateway import accounts as _accounts
from gateway.common.log import log
from gateway.tasks.base import (BILL_BASE, CHAT_BASE, DESKTOP_UA,
                                DESKTOP_ONLY_TASKS, EXPERT_ID_POOL,
                                NIGHT_TASK_CODES, TASK_SPECS, TEAM_ID_POOL,
                                WEB_BASE, WEB_UA, _pause,
                                in_night_window)


def fetch_growth_tasks(account):
    """Fetch the growth task list and current status."""
    if getattr(account, "is_enterprise", lambda: False)():
        return []
    url = CHAT_BASE + "/v2/activity/growth/tasks"
    req = urllib.request.Request(url, headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_tasks = (data.get("data") or {}).get("tasks") or []
            tasks = []
            for t in raw_tasks:
                code = t.get("task_code") or ""
                spec = TASK_SPECS.get(code, {})
                prog = t.get("progress") or {}
                tasks.append({
                    "task_code": code,
                    "name": t.get("title") or spec.get("name") or code,
                    "description": t.get("description") or t.get("task_desc") or "",
                    "jump_url": t.get("jump_url") or "",
                    "status": t.get("accept_status") or "not_accepted",
                    "task_type": t.get("task_type") or "",
                    "current": prog.get("current", 0),
                    "target": prog.get("target", spec.get("target", 1)),
                    "reward_credit": t.get("reward_credit") or spec.get("reward", 0),
                    "reward_energy": t.get("reward_energy", 0),
                    "unforgeable": bool(spec.get("unforgeable")),
                    "reason": spec.get("reason", ""),
                })
            return tasks
    except Exception as exc:
        return []


def fetch_growth_summary(account):
    """Fetch streak, cat-travel and energy state."""
    headers = account.headers("chat")
    out = {"energy": 0, "streak_days": 0, "travel": {"state": "unknown"}}
    # 1. Energy balance
    try:
        req = urllib.request.Request(CHAT_BASE + "/v2/activity/growth/energy", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["energy"] = (d.get("data") or {}).get("balance", 0)
    except Exception as exc:
        log(f"growth/energy query failed: {exc}")
    # 2. Streak check-in
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            st = (d.get("data") or {}).get("streak") or {}
            out["streak_days"] = st.get("days", 0)
    except Exception as exc:
        log(f"growth/streak query failed: {exc}")
    # 3. Cat travel
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status", headers=headers)
        with _accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["travel"] = d.get("data") or {}
    except Exception as exc:
        log(f"buddy/travel/status query failed: {exc}")
    return out


def accept_tasks(account, codes, chunk=20):
    """Accept tasks in batches.

    Returns {"ok": bool, "accepted": [...], "failed": [...], "msg": str}; the
    upstream reports per-task status (accepted / already_accepted / failure).
    """
    out = {"ok": True, "accepted": [], "failed": [], "msg": "", "reasons": []}
    if not codes:
        return out
    url = CHAT_BASE + "/v2/activity/growth/tasks/accept"
    for i in range(0, len(codes), max(1, chunk)):
        part = codes[i:i + max(1, chunk)]
        body = json.dumps({"task_codes": part}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=account.headers("chat"))
        try:
            with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(300).decode("utf-8", "replace")
            except Exception:
                pass
            out["ok"] = False
            out["msg"] = "HTTP %s: %s" % (exc.code, detail[:200])
            out["failed"].extend(part)
            out["reasons"].append(out["msg"])
            log(f"accept batch failed: {out['msg']}")
            continue
        except Exception as exc:
            out["ok"] = False
            out["msg"] = str(exc)[:200]
            out["failed"].extend(part)
            out["reasons"].append(out["msg"])
            log(f"accept batch failed: {exc}")
            continue
        if d.get("code") != 0:
            out["ok"] = False
            out["msg"] = d.get("msg") or ("code=%s" % d.get("code"))
            out["failed"].extend(part)
            out["reasons"].append(out["msg"])
            log(f"accept rejected: {out['msg']}")
            continue
        results = (d.get("data") or {}).get("results") or []
        seen = set()
        for item in results:
            code = item.get("task_code")
            st = str(item.get("status") or "")
            seen.add(code)
            if st in ("accepted", "already_accepted"):
                out["accepted"].append(code)
            else:
                out["failed"].append(code)
                out["reasons"].append("%s: %s" % (code, item.get("message") or st))
                log(f"task {code} accept status={st}")
        for code in part:
            if code not in seen:
                out["failed"].append(code)
    return out


def claim_task(account, code):
    """Claim a task reward; falls back from copilot.tencent.com to workbuddy.cn."""
    url = f"{CHAT_BASE}/activity/growth/tasks/{code}/claim"
    req = urllib.request.Request(url, data=b"{}", method="POST", headers=account.headers("chat"))
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            if d.get("code") == 0:
                data = d.get("data") or {}
                return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
            # 200 with a non-zero code (typically "task not completed": claimed too early).
            log(f"task {code} claim rejected: {d.get('msg')}")
            return {"ok": False, "credit": 0, "energy": 0, "msg": d.get("msg") or f"code={d.get('code')}"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        err_msg = f"HTTP {exc.code}"
        try:
            err_msg = json.loads(body).get("msg") or err_msg
        except Exception:
            pass
        if exc.code == 400:
            # Fall back to the web domain for the claim.
            web_url = f"{WEB_BASE}/activity/growth/tasks/{code}/claim"
            web_hdrs = {
                "Authorization": "Bearer " + account.access_token,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": WEB_BASE,
                "Referer": f"{WEB_BASE}/profile/growth-center",
                "x-client-platform": "web",
                "User-Agent": WEB_UA,
                "X-User-Id": account.uid,
                "X-Domain": WEB_BASE,
            }
            try:
                req_web = urllib.request.Request(web_url, data=b"{}", method="POST", headers=web_hdrs)
                with _accounts.urlopen(req_web, timeout=15, proxy=account.proxy) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("code") == 0:
                        data = d.get("data") or {}
                        return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
                    log(f"task {code} web claim rejected: code={d.get('code')} msg={d.get('msg')}")
                    return {"ok": False, "credit": 0, "energy": 0, "msg": d.get("msg") or err_msg}
            except Exception as exc2:
                log(f"task {code} web claim failed: {exc2}")
        log(f"task {code} claim failed: {err_msg}")
        return {"ok": False, "credit": 0, "energy": 0, "msg": err_msg}
    except Exception as exc:
        log(f"task {code} claim failed: {exc}")
    return {"ok": False, "credit": 0, "energy": 0}


def build_event(account, kind, idx=0, expert=None):
    """Build a canonical event payload for the given kind.

    expert: (expert_id, expert_name). Expert/team events need a fresh id each
    time; the upstream dedupes on (eventCode, id) and repeats never advance.
    """
    now = int(time.time() * 1000)
    cid = f"wb-task-{now}-{idx}"
    rid = f"{cid}-req"
    uid = account.uid

    if kind == "canvas":
        return {"eventCode": "wbx_design_canvas_task_create", "timestamp": now,
                "reportDelay": 0, "conversationId": cid, "requestId": rid,
                "source": "summon_keyword", "isCustomModel": False, "name": "",
                "inputLength": 12, "id": f"wbx-canvas-{now}", "cost": 0,
                "isSuccessful": True, "userId": uid}
    if kind == "template":
        return {"eventCode": "agent_task_created_with_template", "timestamp": now,
                "reportDelay": 0, "isCustomModel": True, "id": str(idx),
                "name": "幻灯片", "requestId": rid, "conversationId": cid, "userId": uid}
    if kind in ("expert", "team", "lighthouse"):
        etype = "team" if kind == "team" else "agent"
        if expert:
            ex_id, name = expert
        elif kind == "lighthouse":
            ex_id, name = "ex_2cvvUZQhDyeJ", "腾讯轻量云专家"
        elif kind == "team":
            ex_id, name = "CloudOpsTeam", "运维专家团队"
        else:
            ex_id, name = "ContentCreator", "内容创作专家"
        return {"eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
                "mode": "CLOUD", "id": ex_id, "name": name, "expertTitle": name,
                "type": "02-Engineering", "expertType": etype, "source": "builtin",
                "version": "1.0.2", "cost": 0, "characterCount": 12, "conversationId": cid,
                "requestId": rid, "messageId": rid, "requestModelId": "deepseek-v4-flash",
                "requestModelName": "DeepSeek V4 Flash", "userId": uid}
    if kind == "skill":
        return {"eventCode": "skill_info", "timestamp": now, "reportDelay": 0,
                "skillId": "skill_2096525080079265792", "name": "pptx", "userId": uid}
    if kind == "automation":
        return {"eventCode": "automated_task_create_suc", "timestamp": now, "reportDelay": 0,
                "name": "每周工作整理", "type": "cron", "source": "manually",
                "modelId": "deepseek-v4-flash", "modelIsThinking": False,
                "conversationId": cid, "requestId": rid,
                "schedule": {"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0"},
                "prompt": "每周五自动整理本周工作", "userId": uid}
    if kind == "playbook":
        return {"eventCode": "playbook_prompt_send", "timestamp": now, "reportDelay": 0,
                "id": "worker-ledger-freedom-dashboard", "name": "打工人小账本",
                "type": "other", "promptLength": 10, "isOfficial": 1,
                "source": "discover", "conversationId": cid, "requestId": rid, "userId": uid}
    if kind == "skin":
        return {"eventCode": "appearance_skin_apply", "timestamp": now, "reportDelay": 0,
                "action": "apply", "source": "settings_close", "id": "theme-tkmw7j",
                "vipLevel": "free", "series": "craft", "type": "unknown",
                "name": "和平精英激战金秋", "userId": uid}
    if kind in ("chat", "glmchat", "cat"):
        m_id = "glm-5.2" if kind in ("glmchat", "cat") else "deepseek-v4-flash"
        m_nm = "GLM-5.2" if kind in ("glmchat", "cat") else "DeepSeek V4 Flash"
        mode = "night" if kind == "cat" else "craft"
        return {"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
                "mode": mode, "conversationId": cid, "requestId": rid,
                "inputLength": 12, "requestModelId": m_id, "requestModelName": m_nm,
                "isPlan": False, "agentName": "default", "agentType": "conversation",
                "userId": uid}
    return {"eventCode": "heartbeat", "timestamp": now, "userId": uid}


def report_events(account, events, base=None):
    """Report an event array upstream.

    Posts to copilot.tencent.com (the chat side), the address the desktop
    client uses; events there advance growth progress in real time.
    """
    if base is None:
        base = CHAT_BASE
    url = base + "/v2/report"
    headers = account.headers("billing" if base == BILL_BASE else "chat")
    body = json.dumps(events).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _accounts.urlopen(req, timeout=15, proxy=account.proxy) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return d.get("code") == 0
    except Exception:
        return False


def _progress_one(account, t, logs, gap):
    """Advance one task; returns credits earned. auto tasks need no acceptance."""
    code = t["task_code"]
    spec = TASK_SPECS.get(code)
    if not spec or spec.get("unforgeable"):
        if spec and spec.get("reason"):
            logs.append(f"⏭ 任务 [{spec.get('name') or code}] 需真实操作完成: {spec['reason']}, 跳过")
        return 0

    status = t["status"]
    cur = t.get("current", 0)
    tgt = t.get("target", 1)

    if status == "claimed":
        return 0

    if status == "completed" or cur >= tgt:
        res = claim_task(account, code)
        if res.get("ok"):
            cr = res.get("credit", 0)
            logs.append(f"✓ 任务 [{spec['name']}] 领奖成功: +{cr} 积分")
            _pause(gap)
            return cr
        logs.append(f"! 任务 [{spec['name']}] 领奖失败: {res.get('msg') or '未知原因'}")
        _pause(gap)
        return 0

    if code in DESKTOP_ONLY_TASKS:
        jump = t.get("jump_url") or "workbuddy://chat"
        logs.append(f"⏭ 任务 [{spec['name']}] 需真实操作完成: {DESKTOP_ONLY_TASKS[code]}"
                    f" (深链 {jump}), 跳过事件伪造")
        return 0

    if code in NIGHT_TASK_CODES and not in_night_window():
        logs.append(f"🌙 任务 [{spec['name']}] 仅 23:00-08:00 上报计数, 当前不在窗口, 跳过"
                    f" (每日 01:00 调度器自动执行, 累计 3 天)")
        return 0

    if status == "not_accepted" and t.get("task_type") != "auto":
        logs.append(f"⏭ 任务 [{spec['name']}] 仍未接取, 跳过 (先解决接取失败)")
        return 0

    need = max(1, tgt - cur)
    kind = spec.get("kind")
    logs.append(f"正在点亮任务 [{spec['name']}] (需上报 {need} 次)...")
    report_ok = True
    id_pool = None
    if kind in ("expert", "team"):
        id_pool = TEAM_ID_POOL if kind == "team" else EXPERT_ID_POOL
    for i in range(need):
        expert = None
        if id_pool:
            pid, pnm = id_pool[(cur + i) % len(id_pool)]
            expert = (pid, pnm)
        ev = build_event(account, kind, idx=i, expert=expert)
        if not report_events(account, [ev]):
            report_ok = False
        if i < need - 1:
            _pause(gap)
    if not report_ok:
        logs.append(f"! 任务 [{spec['name']}] 部分事件上报失败 (上游拒绝), 继续尝试领奖")
    _pause(1.5)

    prog = cur
    for attempt in range(6):
        fresh = next((x for x in fetch_growth_tasks(account) if x["task_code"] == code), None)
        if fresh:
            prog = fresh.get("current", 0)
            if prog >= tgt or fresh.get("status") in ("completed", "claimed"):
                break
            if prog > cur:
                _pause(2.5)
                continue
        if attempt < 2:
            _pause(1.5)
        else:
            break
    if prog < tgt:
        logs.append(f"? 任务 [{spec['name']}] 已上报但进度 {prog}/{tgt} 未达成, 领奖顺延到下次运行")
        _pause(gap)
        return 0

    res = claim_task(account, code)
    if res.get("ok"):
        cr = res.get("credit", 0)
        logs.append(f"✓ 任务 [{spec['name']}] 点亮并领奖成功: +{cr} 积分")
        _pause(gap)
        return cr
    logs.append(f"! 任务 [{spec['name']}] 进度已达 {prog}/{tgt} 但领奖失败: {res.get('msg') or '未知原因'}")
    _pause(gap)
    return 0


def run_growth_tasks(account, gap=1.0):
    """Run the full accept/light/claim cycle for the growth tasks."""
    if account.realm != "cn":
        return {"ok": False, "msg": "国际版不适用国内成长任务中心", "logs": []}
    if getattr(account, "is_enterprise", lambda: False)():
        return {"ok": True, "msg": "企业版账号无成长体系，跳过", "logs": []}

    logs = []
    logs.append(f"开始为账号 {account.nickname or account.uid[:8]} 运行成长任务自动化...")
    tasks = fetch_growth_tasks(account)
    if not tasks:
        logs.append("未能获取到任务清单，请检查网络或账号状态")
        return {"ok": False, "logs": logs, "earned_credit": 0}

    total_earned = 0

    # 0. Auto tasks need no accept and gate the single tasks and travel
    #    (first_buddy grants the Buddy instance): light and claim first.
    for t in tasks:
        if t.get("task_type") == "auto":
            total_earned += _progress_one(account, t, logs, gap)

    # 1. Accept pending tasks (auto tasks are not acceptable; unforgeable are manual).
    def _needs_accept(x):
        return (x["status"] == "not_accepted" and not x.get("unforgeable")
                and x.get("task_type") != "auto")

    unaccepted = [t["task_code"] for t in tasks if _needs_accept(t)]
    if unaccepted:
        logs.append(f"发现 {len(unaccepted)} 个待接取任务，正在批量接取...")
        acc = accept_tasks(account, unaccepted)
        if acc.get("failed"):
            why = acc.get("msg") or ("; ".join(acc.get("reasons") or [])[:160])
            logs.append(f"! 接取未成功 {len(acc['failed'])} 个: {', '.join(acc['failed'][:5])}"
                        + (" ..." if len(acc["failed"]) > 5 else "")
                        + (f" ({why})" if why else ""))
        if acc.get("accepted"):
            logs.append(f"✓ 已接取 {len(acc['accepted'])} 个任务")
        _pause(gap)
        tasks = fetch_growth_tasks(account)
        # Re-check once: the upstream occasionally rejects a whole batch.
        still = [t["task_code"] for t in tasks if _needs_accept(t)]
        if still:
            logs.append(f"仍有 {len(still)} 个未接取，重试接取一次...")
            retry = accept_tasks(account, still)
            if retry.get("accepted"):
                logs.append(f"✓ 重试接取成功 {len(retry['accepted'])} 个")
            _pause(gap)
            tasks = fetch_growth_tasks(account)
        if not tasks:
            logs.append("! 接取后无法获取任务清单，本轮中止")
            return {"ok": False, "logs": logs, "earned_credit": total_earned}
        still_pending = [t["task_code"] for t in tasks if _needs_accept(t)]
        if still_pending:
            logs.append(f"! 仍有 {len(still_pending)} 个任务处于未接取状态，"
                        f"对未接取任务上报事件不会计入进度，本轮跳过这些任务")

    # 2. Process each task (auto already handled in step 0).
    for t in tasks:
        if t.get("task_type") == "auto":
            continue
        total_earned += _progress_one(account, t, logs, gap)

    # 4. Also check cat travel.
    from gateway.tasks.travel import do_cat_travel
    tr_res = do_cat_travel(account)
    if tr_res.get("msg"):
        logs.append(f"猫猫日常: {tr_res.get('msg')}")
        if tr_res.get("credit"):
            total_earned += tr_res.get("credit", 0)

    # 5. Refresh the credit balance.
    account.fetch_credits()
    logs.append(f"🎉 全部完成！本次累计新增到账: +{total_earned} 积分，当前总剩余: {account.credits.get('remain', 0)} 积分")
    return {"ok": True, "logs": logs, "earned_credit": total_earned, "credits": account.credits}


def run_night_growth(account):
    """Night-owl task (black_cat): report one GLM-5.2 night-chat event between
    23:00 and 08:00, once a day, claimable after 3 days. Daytime calls skip."""
    if account.realm != "cn":
        return {"ok": False, "msg": "国际版不适用国内成长任务中心", "logs": [], "earned_credit": 0}
    if getattr(account, "is_enterprise", lambda: False)():
        return {"ok": True, "msg": "企业版账号无成长体系，跳过", "logs": [], "earned_credit": 0}
    logs = []
    name = account.nickname or account.uid[:8]
    if not in_night_window():
        return {"ok": True, "earned_credit": 0, "logs": [
            f"[{name}] 当前不在 23:00-08:00 夜间窗口, 夜猫子任务跳过 (每日 01:00 自动执行)"]}
    tasks = fetch_growth_tasks(account)
    t = next((x for x in tasks if x["task_code"] == "black_cat"), None)
    if not t:
        return {"ok": False, "earned_credit": 0, "logs": [f"[{name}] 未获取到夜猫子任务清单"]}
    if t["status"] == "claimed":
        return {"ok": True, "earned_credit": 0, "logs": [f"[{name}] 夜猫子任务已领奖"]}
    if t["status"] == "not_accepted":
        # The upstream counts progress only for accepted tasks; this one too.
        acc_res = accept_tasks(account, ["black_cat"])
        if not acc_res.get("accepted"):
            why = acc_res.get("msg") or ("; ".join(acc_res.get("reasons") or [])[:120])
            logs.append(f"[{name}] 夜猫子任务未接取, 今晚跳过 (先解决接取失败{': ' + why if why else ''})")
            return {"ok": False, "earned_credit": 0, "logs": logs}
        logs.append(f"[{name}] 夜猫子任务已接取")
    cur, tgt = t.get("current", 0), t.get("target", 3)
    if cur >= tgt:
        res = claim_task(account, "black_cat")
        cr = res.get("credit", 0) if res.get("ok") else 0
        account.fetch_credits()
        logs.append(f"[{name}] 夜猫子任务达标, 领奖 {'✓ +' + str(cr) + ' 积分' if res.get('ok') else '! 失败: ' + (res.get('msg') or '')}")
        return {"ok": res.get("ok", False), "earned_credit": cr, "logs": logs}
    ev = build_event(account, "cat", idx=0)
    ok = report_events(account, [ev])
    logs.append(f"[{name}] 上报夜间 GLM-5.2 对话事件: {'成功' if ok else '失败'} (进度 {cur}/{tgt})")
    fresh = None
    for _ in range(4):
        _pause(4)
        fresh = next((x for x in fetch_growth_tasks(account) if x["task_code"] == "black_cat"), None)
        if fresh and fresh.get("current", 0) > cur:
            break
    new_cur = fresh.get("current", cur) if fresh else cur
    if new_cur >= tgt:
        res = claim_task(account, "black_cat")
        cr = res.get("credit", 0) if res.get("ok") else 0
        logs.append(f"[{name}] 夜猫子任务完成 {new_cur}/{tgt}, 领奖 {'✓ +' + str(cr) + ' 积分' if res.get('ok') else '! 失败: ' + (res.get('msg') or '')}")
    elif new_cur > cur:
        logs.append(f"[{name}] 今晚 +1 ({new_cur}/{tgt}), 明晚继续, 累计 3 天可领奖")
    else:
        logs.append(f"[{name}] 已上报但进度暂未变化 ({new_cur}/{tgt}), 明晚调度器会继续累计")
    account.fetch_credits()
    earned = 0
    for line in logs:
        m = re.search(r"\+(\d+) 积分", line)
        if m:
            earned = int(m.group(1))
    return {"ok": True, "earned_credit": earned, "logs": logs}
