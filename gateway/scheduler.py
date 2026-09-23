"""scheduler.py —— 后台定时调度器（默认关闭 + 每日随机偏移）

负责常驻后台自动执行：
1. Token 保活：定期检查 Token 剩余寿命，不足 2 小时自动调用 Refresh Token。
2. 每日签到：每日定时为所有国内版账号自动签到领积分。
3. 猫猫旅行：自动派出或领取归来奖励。
4. 夜猫子任务：01:00 前后上报 black_cat 事件。

反风控改动（相对源项目 wb_scheduler.py）：
- enabled 默认 False：手动 wbctl checkin / 直接调用不受限，但常驻排程
  必须显式 Scheduler(enabled=True).start()（或 server --tasks）。
- 每个排程时刻每天随机偏移 ±15~45 分钟（分钟级），当日偏移落盘
  accounts/scheduler-state.json：固定 09:00:00 整点触发本身就是行为特征。
- 账号之间 3~10s 随机间隔（源为固定 1.0s）。
"""
import json
import os
import random
import tempfile
import threading
import time

from gateway import tasks as wb_tasks
from gateway.tasks import do_cat_travel

STATE_NAME = "scheduler-state.json"


class Scheduler:
    def __init__(self, pool, log=None, enabled=False, state_dir=None):
        self.pool = pool
        self._external_log = log or (lambda msg, **kw: None)
        # 对齐官方默认排程基准小时 (CST 24h)；实际触发时刻每天加随机偏移
        self.checkin_hours = [9, 21]
        self.travel_hours = [9, 21]
        self.keepalive_hours = [22]
        self.cat_hours = [1]
        self.all_hours = sorted(set(self.checkin_hours + self.travel_hours
                                    + self.keepalive_hours + self.cat_hours))
        self.enabled = enabled
        self.state_dir = state_dir or (pool.dir if pool is not None else None)
        self._stop_event = threading.Event()
        self._thread = None
        self.last_run_time = None
        self.next_run_time = None
        self.logs = []
        # Guards against overlapping runs: a manual trigger can land on top
        # of the hourly job.
        self._run_lock = threading.Lock()
        self._offsets = {}
        self._last_fired = set()  # "YYYY-MM-DD HH" markers, in-process dedupe
        self._load_state()
        self._ensure_today_offsets()
        # Surface task-level failures (dead endpoints, upstream shape changes)
        # in the same log.
        wb_tasks.set_logger(self.log)

    # ---------------- logging ----------------
    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append("[%s] %s" % (ts, msg))
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        try:
            self._external_log("[调度器] %s" % msg)
        except Exception:
            pass

    # ---------------- daily random offsets ----------------
    def _state_path(self):
        return os.path.join(self.state_dir, STATE_NAME) if self.state_dir else None

    def _load_state(self):
        path = self._state_path()
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("day") == time.strftime("%Y-%m-%d"):
                offsets = data.get("offsets") or {}
                self._offsets = {int(h): int(m) for h, m in offsets.items()}
                if self._offsets:
                    self._offset_day = data["day"]
        except Exception:
            pass

    def _save_state(self):
        path = self._state_path()
        if not path:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            payload = {"day": time.strftime("%Y-%m-%d"),
                       "offsets": {str(h): m for h, m in self._offsets.items()}}
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".sched.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception as exc:
            self.log("调度状态落盘失败: %s" % exc)

    def _ensure_today_offsets(self):
        """±15~45 分钟偏移，每日每个整点一次；当日重启从 state 恢复不漂移。"""
        day = time.strftime("%Y-%m-%d")
        if getattr(self, "_offset_day", None) == day and self._offsets:
            return
        self._offsets = {h: random.choice((-1, 1)) * random.randint(15, 45)
                         for h in self.all_hours}
        self._offset_day = day
        self._save_state()

    def _fire_minute(self, hour):
        """Absolute minute-of-day when `hour`:00 fires today (offset applied)."""
        self._ensure_today_offsets()
        return hour * 60 + self._offsets.get(hour, 0)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if not self.enabled:
            self.log("调度器未启用 (enabled=False)，不启动后台线程；"
                     "wbctl checkin / run_growth_tasks 仍可手动触发")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        self._stop_event.set()

    def _run_loop(self):
        # 启动后先休眠 10 秒等待主服务就绪，然后执行初次巡检
        time.sleep(10)
        try:
            self._execute_cycle("启动初次初始化巡检")
        except Exception as exc:
            self.log("初次巡检异常: %s" % exc)

        while not self._stop_event.is_set():
            try:
                if self.enabled:
                    self._check_due()
            except Exception as exc:
                self.log("排程执行异常: %s" % exc)
            self._stop_event.wait(30)

    def _check_due(self):
        """30 秒轮询：当前时间命中任一 (hour, offset) 触发分钟则执行。"""
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        day = time.strftime("%Y-%m-%d")
        for h in self.all_hours:
            target = self._fire_minute(h)
            # 跨日边界（23:45~00:15 的偏移）用 ±12h 归一比较
            delta = (minute - target + 720) % 1440 - 720
            key = "%s %02d" % (day, h)
            if key in self._last_fired:
                continue
            if 0 <= delta < 2:  # 命中触发分钟（轮询 30s，容忍 2 分钟窗口）
                self._last_fired.add(key)
                self._execute_cycle("随机偏移排程命中 (%02d:%02d)"
                                    % (target // 60, target % 60), fired_hour=h)
                return
        self._calc_next_fire()

    def _calc_next_fire(self):
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        candidates = []
        for h in self.all_hours:
            target = self._fire_minute(h)
            delta = (target - minute + 1440) % 1440
            candidates.append((delta, target))
        delta, target = min(candidates)
        when = time.time() + delta * 60
        t = time.localtime(when)
        self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t)

    def trigger_now(self):
        """手动立即触发一次调度检查（无视 enabled 开关）。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle, args=("手动立即触发",),
                         daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检", fired_hour=None):
        if not self._run_lock.acquire(blocking=False):
            self.log("跳过本次巡检 (%s)：上一轮仍在执行" % trigger_reason)
            return
        try:
            self._run_cycle(trigger_reason, fired_hour)
        finally:
            self._run_lock.release()

    def _run_cycle(self, trigger_reason="周期巡检", fired_hour=None):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log("开始执行任务 (%s)..." % trigger_reason)
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            return

        refreshed_count = 0
        checkin_count = 0
        travel_count = 0

        for acc in list(self.pool.accounts):
            uid8 = acc.uid[:8] if acc.uid else "?"
            # 1. Token 剩余寿命不足 2 小时主动刷新保活
            exp = acc.expires_at or 0
            if exp and (exp - time.time()) < 7200:
                self.log("账号 [%s] Token 即将到期，执行主动保活刷新..." % uid8)
                if acc.refresh():
                    refreshed_count += 1
                    self.log("✓ 账号 [%s] Token 自动保活刷新成功" % uid8)
                else:
                    self.log("! 账号 [%s] Token 保活刷新失败: %s" % (uid8, acc.last_error))

            # 2. 国内版：每日签到与猫猫旅行
            if acc.realm == "cn":
                if acc.can_checkin():
                    self.log("检测到国内版账号 [%s] 今日尚未签到，执行自动签到..." % uid8)
                    res = acc.checkin()
                    if res.get("ok"):
                        checkin_count += 1
                        self.log("✓ 账号 [%s] 自动签到成功: %s" % (uid8, res.get("msg")))
                    else:
                        self.log("! 账号 [%s] 自动签到未成功: %s"
                                 % (uid8, res.get("error") or res.get("msg")))
                    wb_tasks._pause(1.0)

                travel = do_cat_travel(acc)
                if travel.get("action") in ("claim", "depart"):
                    travel_count += 1
                    self.log("账号 [%s] 猫猫日常处理: %s" % (uid8, travel.get("msg")))
                # 夜猫子专属任务：仅在 cat_hours 命中的那轮执行。偏移只动
                # 分钟不动小时（±45min 内），小时匹配即可靠；手动触发不带
                # fired_hour 时退回墙钟判断。
                hour_now = time.localtime().tm_hour
                if (fired_hour in self.cat_hours if fired_hour is not None
                        else hour_now in self.cat_hours):
                    night = wb_tasks.run_night_growth(acc)
                    for line in night.get("logs", []):
                        self.log("夜猫: %s" % line)
                # 账号之间 3~10s 随机（源为固定 1.0s）
                time.sleep(random.uniform(3.0, 10.0))

        self.log("巡检完成：Token保活 %d 个，每日签到 %d 个，猫猫日常 %d 个"
                 % (refreshed_count, checkin_count, travel_count))

    def status(self):
        return {
            "enabled": self.enabled,
            "mode": "每日随机偏移排程 (基准 09:00/21:00 签到旅行 · 22:00 保活 · 01:00 夜猫，±15~45min)",
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "logs": self.logs[-20:],
        }
