"""Tests for the module-layout refactor and its two behaviour changes.
Facade transparency, catalog data files, uid/prefix resolution, growthplan."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))

from gateway import accounts as ac  # noqa: E402
from gateway import catalog  # noqa: E402
from gateway import translate as tr  # noqa: E402
from gateway import upstream as up  # noqa: E402
from gateway.account import pool as pool_mod  # noqa: E402
from gateway.account import transport as transport_mod  # noqa: E402
from gateway.account.model import Account  # noqa: E402
from gateway.protocol import state as proto_state  # noqa: E402
from gateway.upstream import state as up_state  # noqa: E402
from helpers_jwt import make_jwt  # noqa: E402


class CatalogExternal(unittest.TestCase):
    """The model tables moved from source to data/*.json."""

    def test_tables_load_from_data_dir(self):
        self.assertEqual(len(catalog.STATIC_INTL_MODELS), 24)
        self.assertEqual(len(catalog.STATIC_CN_MODELS), 39)
        self.assertIs(catalog.STATIC_MODELS, catalog.STATIC_INTL_MODELS)

    def test_data_dir_holds_plain_json(self):
        for name in ("catalog_intl", "catalog_cn"):
            path = os.path.join(_ROOT, "gateway", "data", name + ".json")
            with open(path, encoding="utf-8") as fh:
                self.assertIsInstance(json.load(fh), list)

    def test_entries_keep_their_fields(self):
        first = catalog.STATIC_CN_MODELS[0]
        self.assertEqual(first["id"], "hy3")
        self.assertIn("maxOutputTokens", first)


class FacadeTransparency(unittest.TestCase):
    """Module facades must stay writable by callers and tests."""

    def test_translate_state_write_reaches_protocol_state(self):
        old = proto_state.CURRENT_REALM
        try:
            tr.CURRENT_REALM = "cn"
            self.assertEqual(proto_state.CURRENT_REALM, "cn")
            self.assertEqual(tr.CURRENT_REALM, "cn")
        finally:
            tr.CURRENT_REALM = old

    def test_translate_system_prompt_write_proxies(self):
        old = proto_state.SYSTEM_PROMPT
        try:
            tr.SYSTEM_PROMPT = "hello"
            self.assertEqual(proto_state.SYSTEM_PROMPT, "hello")
        finally:
            tr.SYSTEM_PROMPT = old

    def test_translate_model_cache_is_shared(self):
        self.assertIs(tr._models_cache, proto_state.MODELS_CACHE)

    def test_accounts_urlopen_patch_forwards_to_transport(self):
        sentinel = object()
        with mock.patch.object(ac, "urlopen", sentinel):
            self.assertIs(transport_mod.urlopen, sentinel)

    def test_upstream_pool_write_reaches_state(self):
        old = up_state._POOL
        try:
            marker = object()
            up._POOL = marker
            self.assertIs(up_state._POOL, marker)
        finally:
            up._POOL = old

    def test_upstream_usage_dir_write_reaches_state(self):
        old = up_state.USAGE_DIR
        try:
            up.USAGE_DIR = "/tmp/nope"
            self.assertEqual(up_state.USAGE_DIR, "/tmp/nope")
        finally:
            up.USAGE_DIR = old


class ResolveAccount(unittest.TestCase):
    """AccountPool.resolve: exact uid, unique prefix, unique nickname."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="resolve-")
        self.pool = pool_mod.AccountPool(self.dir)
        self.a = Account({"uid": "aaaaaaaa-1111", "nickname": "alpha",
                          "accessToken": make_jwt(uid="aaaaaaaa-1111")})
        self.b = Account({"uid": "bbbbbbbb-2222", "nickname": "beta",
                          "accessToken": make_jwt(uid="bbbbbbbb-2222")})
        self.pool.add(self.a)
        self.pool.add(self.b)

    def test_full_uid(self):
        got, err = self.pool.resolve("aaaaaaaa-1111")
        self.assertIsNone(err)
        self.assertIs(got, self.a)

    def test_unique_prefix(self):
        got, err = self.pool.resolve("aaaa")
        self.assertIsNone(err)
        self.assertIs(got, self.a)

    def test_unique_nickname(self):
        got, err = self.pool.resolve("beta")
        self.assertIsNone(err)
        self.assertIs(got, self.b)

    def test_ambiguous_prefix_is_an_error(self):
        got, err = self.pool.resolve("")
        self.assertIsNone(got)
        self.assertTrue(err)

    def test_unknown_needle_is_an_error(self):
        got, err = self.pool.resolve("zeta")
        self.assertIsNone(got)
        self.assertIn("no account", err)

    def test_nickname_collision_never_picks_one(self):
        self.b.nickname = "alpha"
        got, err = self.pool.resolve("alpha")
        self.assertIsNone(got)
        self.assertIn("ambiguous nickname", err)

    def test_exact_uid_beats_prefix(self):
        # A uid that is itself a prefix of another must still resolve exactly.
        c = Account({"uid": "aaaa", "nickname": "short",
                     "accessToken": make_jwt(uid="aaaa")})
        self.pool.add(c)
        got, err = self.pool.resolve("aaaa")
        self.assertIsNone(err)
        self.assertIs(got, c)


class GrowthplanCommand(unittest.TestCase):
    """growthplan runs the growth engine once, outside the scheduler."""

    def test_subcommand_registered(self):
        import argparse
        from gateway.cli import wbctl
        parser = None
        captured = {}

        def fake_parse(self, argv=None):
            captured["argv"] = argv
            captured["choices"] = self._subparsers._group_actions[0].choices
            raise SystemExit(0)

        with mock.patch.object(argparse.ArgumentParser, "parse_args", fake_parse):
            try:
                wbctl.main(["growthplan", "someuid"])
            except SystemExit:
                pass
        self.assertIn("growthplan", captured["choices"])

    def test_cmd_calls_run_growth_tasks(self):
        from gateway.cli import wbctl
        called = {}

        class FakePool(object):
            dir = tempfile.mkdtemp()

            def resolve(self, needle):
                acc = Account({"uid": "u1", "accessToken": make_jwt(uid="u1")})
                return acc, None

        class Args(object):
            uid = "u1"
            dir = "/tmp"

        import gateway.tasks as wb_tasks

        def fake_run(acc, **k):
            called["hit"] = True
            return {"ok": True, "logs": [], "earned": 0}

        with mock.patch.object(wbctl, "_pool", lambda a: FakePool()), \
             mock.patch.object(wb_tasks, "run_growth_tasks", fake_run):
            rc = wbctl.cmd_growthplan(Args())
        self.assertEqual(rc, 0)
        self.assertTrue(called.get("hit"))


class SchedulerKeepsDisabledAlive(unittest.TestCase):
    """enable/disable gates inference only; keep-alive ignores it."""

    def test_cycle_visits_disabled_accounts(self):
        from gateway.tasks.scheduler import Scheduler

        class Acc(object):
            uid = "u1"
            realm = "cn"
            enabled = False
            expires_at = 9_999_999_999  # far future; skip the token refresh

            def __init__(self):
                self.refreshed = False
                self.visited = False

            def refresh(self):
                self.refreshed = True
                return True

            def soon_expiring(self):
                return False

            def can_checkin(self):
                self.visited = True
                return True

            def checkin(self):
                return {"ok": True, "msg": "x"}

            def can_daily_chat(self):
                return False

            def headers(self, purpose=None):
                return {}

        class Pool(object):
            dir = tempfile.mkdtemp()
            accounts = [Acc()]

        sched = Scheduler(Pool(), enabled=True)
        import gateway.tasks.scheduler as sched_mod
        # Pin the check-in hour to "now": the cycle only visits accounts for
        # this leg when the wall clock matches one of its configured hours.
        sched.checkin_hours = [time.localtime().tm_hour]
        with mock.patch.object(sched, "_load_state"), \
             mock.patch.object(sched, "_ensure_today_offsets"), \
             mock.patch.object(sched_mod, "do_cat_travel",
                               lambda acc: {"action": "idle"}):
            sched._run_cycle("test")
        self.assertTrue(Pool.accounts[0].visited)


if __name__ == "__main__":
    unittest.main()
