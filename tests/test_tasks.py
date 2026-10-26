"""Offline tests for gateway.tasks / gateway.scheduler.
Every upstream call is intercepted by a fake urlopen; sleep is recorded."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers_jwt import make_jwt  # noqa: E402

from gateway import accounts as acct_mod  # noqa: E402
from gateway.accounts import Account, AccountPool  # noqa: E402
from gateway import tasks as tk  # noqa: E402
from gateway import scheduler as sd  # noqa: E402


class UpstreamRecorder:
    """Fake urllib opener: scripted responses keyed by URL substring."""

    def __init__(self, rules, default=None):
        self.rules = rules  # [(substr, dict | callable(url,data) | Exception)]
        self.default = default if default is not None else {"code": 0, "data": {}}
        self.calls = []

    def __call__(self, req, timeout=30, proxy=""):
        url = req.full_url
        body_data = req.data
        self.calls.append((url, body_data))
        for substr, resp in self.rules:
            if substr in url:
                if isinstance(resp, Exception):
                    raise resp
                if callable(resp):
                    resp = resp(url, body_data)
                if isinstance(resp, dict):
                    resp = json.dumps(resp).encode()
                r = mock.MagicMock()
                r.read.return_value = resp
                r.__enter__.return_value = r
                return r
        r = mock.MagicMock()
        r.read.return_value = json.dumps(self.default).encode()
        r.__enter__.return_value = r
        return r


def make_cn_account(dirpath):
    a = Account({"accessToken": make_jwt(uid="cn-uid",
                                         iss="https://copilot.tencent.com"),
                 "nickname": "cn", "refreshToken": "rt"})
    a.save(dirpath)
    return a


class PauseIsolation(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        mock.patch.object(time, "sleep", side_effect=self.sleeps.append).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_pause_jitters_above_floor(self):
        tk._pause(1.0)
        self.assertGreaterEqual(self.sleeps[0], 1.0)

    def test_pause_uses_randomness(self):
        vals = set()
        for _ in range(20):
            self.sleeps.clear()
            tk._pause(1.0)
            vals.add(round(self.sleeps[0], 3))
        self.assertGreater(len(vals), 5)  # not a constant cadence


class TaskEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.account = make_cn_account(self.tmp.name)
        self.sleeps = []
        mock.patch.object(time, "sleep", side_effect=self.sleeps.append).start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_event_kinds_build_distinct_ids(self):
        e1 = tk.build_event(self.account, "expert", idx=0, expert=("ex_a", "A"))
        e2 = tk.build_event(self.account, "expert", idx=1, expert=("ex_b", "B"))
        self.assertEqual(e1["eventCode"], "expert_actual_use")
        self.assertNotEqual(e1["id"], e2["id"])  # (eventCode,id) dedupe defence
        self.assertEqual(tk.build_event(self.account, "automation")["eventCode"],
                         "automated_task_create_suc")

    def test_accept_reports_failed_batch(self):
        def handler(url, data):
            return {"code": 0, "data": {"results": [
                {"task_code": "chat_5", "status": "accepted"},
                {"task_code": "skill_1", "status": "nope"}]}}
        up = UpstreamRecorder([("/tasks/accept", handler)])
        with mock.patch.object(acct_mod, "urlopen", up):
            res = tk.accept_tasks(self.account, ["chat_5", "skill_1", "missing"])
        self.assertEqual(res["accepted"], ["chat_5"])
        self.assertIn("skill_1", res["failed"])
        self.assertIn("missing", res["failed"])

    def test_claim_falls_back_to_web_domain(self):
        err = urllib.error.HTTPError("http://x", 400, "bad", {},
                                     io.BytesIO(b'{"msg":"task not completed"}'))

        def primary(url, data):
            raise err

        up = UpstreamRecorder([
            ("copilot.tencent.com/activity/growth/tasks/chat_5/claim", primary),
            ("www.workbuddy.cn/activity/growth/tasks/chat_5/claim",
             {"code": 0, "data": {"credit": 100}}),
        ])
        with mock.patch.object(acct_mod, "urlopen", up):
            res = tk.claim_task(self.account, "chat_5")
        self.assertTrue(res["ok"])
        self.assertEqual(res["credit"], 100)

    def test_travel_sends_location_id(self):
        def depart(url, data):
            body = json.loads(data)
            if body.get("location_id") == 7:
                return {"code": 0, "data": {"location": {"name": "海边"}}}
            return {"code": 400, "msg": "bad"}

        up = UpstreamRecorder([
            ("/travel/status", {"code": 0, "data": {"state": "idle",
                                                    "daily_limit_reached": False}}),
            ("/travel/config", {"code": 0, "data":
                                {"locations": [{"id": 7, "name": "海边"}]}}),
            ("/travel/depart", depart),
        ])
        with mock.patch.object(acct_mod, "urlopen", up):
            res = tk.do_cat_travel(self.account)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "depart")
        self.assertIn("海边", res["msg"])

    def test_travel_claim_on_arrived(self):
        up = UpstreamRecorder([
            ("/travel/status", {"code": 0, "data": {"state": "arrived"}}),
            ("/travel/claim", {"code": 0, "data": {"reward_credit": 50}}),
            ("get-user-resource",
             {"data": {"Response": {"Data": {"Accounts": []}}}}),
        ])
        with mock.patch.object(acct_mod, "urlopen", up):
            res = tk.do_cat_travel(self.account)
        self.assertEqual(res["action"], "claim")
        self.assertEqual(res["credit"], 50)

    def test_run_growth_skips_intl(self):
        intl = Account({"accessToken": make_jwt(uid="i")})
        res = tk.run_growth_tasks(intl)
        self.assertFalse(res["ok"])

    def test_full_cycle_report_then_poll_then_claim(self):
        state = {"progress": 0}

        def tasks_handler(url, data):
            return {"code": 0, "data": {"tasks": [{
                "task_code": "chat_5", "accept_status": "in_progress",
                "progress": {"current": state["progress"], "target": 2},
                "reward_credit": 100}]}}

        def report_handler(url, data):
            state["progress"] = 2  # upstream lands progress after the report
            return {"code": 0}

        up = UpstreamRecorder([
            ("/growth/tasks/accept", {"code": 0, "data": {"results": []}}),
            ("/v2/report", report_handler),
            ("get-user-resource",
             {"data": {"Response": {"Data": {"Accounts": []}}}}),
            ("/claim", {"code": 0, "data": {"credit": 200}}),
            ("/growth/tasks", tasks_handler),
        ])
        with mock.patch.object(acct_mod, "urlopen", up):
            res = tk.run_growth_tasks(self.account)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["earned_credit"], 200)
        paths = [c[0] for c in up.calls]
        report_i = next(i for i, p in enumerate(paths) if "/v2/report" in p)
        claim_i = next(i for i, p in enumerate(paths) if "/claim" in p)
        self.assertLess(report_i, claim_i)  # poll-before-claim ordering

    def test_desktop_only_tasks_honest_skip(self):
        def tasks_handler(url, data):
            return {"code": 0, "data": {"tasks": [{
                "task_code": "RichMeow_Chat", "accept_status": "in_progress",
                "progress": {"current": 0, "target": 1},
                "jump_url": "workbuddy://chat"}]}}
        up = UpstreamRecorder([
            ("/growth/tasks", tasks_handler),
            ("get-user-resource",
             {"data": {"Response": {"Data": {"Accounts": []}}}}),
        ])
        with mock.patch.object(acct_mod, "urlopen", up):
            res = tk.run_growth_tasks(self.account)
        joined = "\n".join(res["logs"])
        self.assertIn("需真实操作", joined)
        self.assertFalse(any("/v2/report" in c[0] for c in up.calls))


class SchedulerBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sleeps = []
        mock.patch.object(time, "sleep", side_effect=self.sleeps.append).start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def _sched(self, enabled=False):
        pool = AccountPool(self.tmp.name)
        return sd.Scheduler(pool, enabled=enabled, state_dir=self.tmp.name)

    def test_disabled_start_spawns_no_thread(self):
        s = self._sched(enabled=False)
        before = threading.active_count()
        s.start()
        self.assertEqual(threading.active_count(), before)

    def test_enabled_start_spawns_thread_and_stops(self):
        s = self._sched(enabled=True)
        before = threading.active_count()
        s.start()
        # schedule thread plus the web-session reclaim watchdog
        self.assertEqual(threading.active_count(), before + 2)
        s.stop()
        s._thread.join(timeout=5)
        s._reclaim_thread.join(timeout=5)
        self.assertFalse(s._thread.is_alive())
        self.assertFalse(s._reclaim_thread.is_alive())

    def test_offsets_in_band_and_persisted(self):
        s = self._sched()
        for h in s.all_hours:
            self.assertTrue(15 <= abs(s._offsets[h]) <= 45, s._offsets[h])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, sd.STATE_NAME)))
        s2 = sd.Scheduler(AccountPool(self.tmp.name), state_dir=self.tmp.name)
        self.assertEqual(s._offsets, s2._offsets)  # restored, not re-rolled

    def test_due_hit_fires_once_per_day(self):
        s = self._sched()
        fired = []
        mock.patch.object(s, "_execute_cycle",
                          side_effect=lambda reason, fired_hour=None:
                          fired.append(fired_hour)).start()
        target_hour = s.all_hours[0]
        s._offsets[target_hour] = 0
        mock.patch.object(time, "localtime", return_value=time.struct_time(
            (2026, 9, 23, target_hour, 0, 30, 0, 1, -1))).start()
        s._check_due()
        s._check_due()
        self.assertEqual(fired, [target_hour])

    def test_manual_trigger_on_empty_pool(self):
        s = self._sched(enabled=False)
        res = s.trigger_now()  # manual bypasses the enabled switch
        self.assertTrue(res["ok"])
        s._run_cycle("direct")  # empty pool: returns without raising
        self.assertIn("暂无可用", "\n".join(s.logs))


class WebAgentSetModel(unittest.TestCase):
    # hub de4fb53: the manifest model is decorative; session/set_model pins it.
    class Chan:
        def __init__(self, link, token, ua, log=None, proxy=""):
            self.requests = []
            self.updates = 0
            self.chunks = 0
        def open(self): pass
        def close(self): pass
        def request(self, method, params, rid):
            self.requests.append((method, rid))
            if method == "session/set_model" and params.get("modelId") == "boom":
                raise RuntimeError("set_model exploded")

    def _run(self, model):
        from gateway import webagent as wa
        chan = {}
        def factory(*a, **k):
            c = self.Chan(*a, **k)
            chan["c"] = c
            return c
        res = wa.run_turn("l", "t", "s", "/w", "Hi", "ua", poll_status=None,
                          wait_seconds=1, poll_interval=0, proxy="",
                          channel_factory=factory, model=model)
        return chan["c"], res

    def test_sequence_with_model(self):
        c, _ = self._run("hy3")
        self.assertEqual([m for m, _ in c.requests],
                         ["initialize", "session/load", "session/set_model",
                          "session/prompt"])
        self.assertEqual([r for _, r in c.requests], [1, 2, 3, 4])

    def test_sequence_without_model_unchanged(self):
        c, _ = self._run(None)
        self.assertEqual([m for m, _ in c.requests],
                         ["initialize", "session/load", "session/prompt"])
        self.assertEqual([r for _, r in c.requests], [1, 2, 4])  # prompt id fixed at 4

    def test_set_model_failure_does_not_kill_the_turn(self):
        c, res = self._run("boom")
        self.assertIn("session/prompt", [m for m, _ in c.requests])
        self.assertFalse(res["ok"])  # no poll_status -> timeout path, but ran on


class LogSinkReentry(unittest.TestCase):
    """A sink that calls log() again must not re-enter itself: the scheduler
    used to install its own log as the process sink, which recursed ~500 deep
    and prefixed every line with hundreds of "[调度器]"."""

    def test_sink_calling_log_writes_one_line(self):
        from gateway.common import log as log_mod
        seen = []

        def sink(msg, level=None):
            seen.append(msg)
            log_mod.log("inner " + msg)
            return True

        old = log_mod._sink
        log_mod.set_logger(sink)
        self.addCleanup(log_mod.set_logger, old)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            log_mod.log("outer")
        self.assertEqual(seen, ["outer"])
        self.assertEqual(out.getvalue(), "[wb2api] inner outer\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
