"""Gateway entry point: argument parsing and startup."""

import argparse
import os
import sys
import threading

from gateway import settings, translate, upstream
from gateway.account.pool import AccountPool
from gateway.common.log import log, set_logger
from gateway.http.state import refresh_pool
from gateway.http import state as _state
from gateway.http.runner import GatewayServer, build_server
from gateway.http.handler import Handler




def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="wb2api minimal WorkBuddy -> OpenAI gateway (no dashboard)")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8788"))
    ap.add_argument("--api-key", default=None,
                    help="persist to accounts/settings.json (single key; empty keeps auth off)")
    ap.add_argument("--accounts-dir", default=_state.ACCOUNTS_DIR)
    ap.add_argument("--usage-dir", default=_state.USAGE_DIR)
    ap.add_argument("--realm", choices=("intl", "cn"), default=None,
                    help="initial upstream exit (default: persisted or 'intl')")
    ap.add_argument("--system-prompt", default=translate.DEFAULT_SYSTEM_PROMPT,
                    help="neutral placeholder (passthrough) or the custom/append "
                         "prompt text when no prompt.file is configured (#160)")
    ap.add_argument("--system-prompt-file", default=None,
                    help="read the custom/append prompt from this file")
    ap.add_argument("--cors", action="store_true",
                    help="send Access-Control-Allow-Origin: * on API routes")
    ap.add_argument("--tasks", action="store_true",
                    help="enable the background scheduler (checkin/cat-travel/keepalive)")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    _state.ARGS = args
    _state.ACCOUNTS_DIR = args.accounts_dir
    _state.USAGE_DIR = args.usage_dir
    translate.set_system_prompt(args.system_prompt)
    translate.PROMPT_FILE_OVERRIDE = args.system_prompt_file or ""

    _state.POOL = AccountPool(_state.ACCOUNTS_DIR, log=log)
    _state.POOL.load()
    # settings/usage switches live next to the resolved dirs, not the
    # import-time defaults (same trap the realm-state file used to have)
    upstream._POOL = _state.POOL
    upstream.USAGE_DIR = args.usage_dir
    translate.ACCOUNTS_DIR = args.accounts_dir

    if args.api_key is not None:
        if args.api_key:
            settings.set_api_key(_state.ACCOUNTS_DIR, args.api_key)
        else:
            settings.clear_api_key(_state.ACCOUNTS_DIR)

    persisted = upstream.load_persisted_realm(_state.ACCOUNTS_DIR)
    realm = args.realm or persisted or os.environ.get("WB_PROXY_DEFAULT_REALM") or "intl"
    upstream.set_realm(realm)
    if persisted != realm:
        upstream.save_persisted_realm(_state.ACCOUNTS_DIR, realm)

    scheduler = None
    refresher = None
    if args.tasks:
        from gateway import scheduler as scheduler_mod
        scheduler = scheduler_mod.Scheduler(_state.POOL, log=log, enabled=True)
        scheduler.start()
        # #174: the expiring-credits dispatch preference reads account.credits,
        # which only the check-in paths used to refresh. One billing call per
        # tick, oldest balance first; disabled by credits_refresh_hours=0.
        from gateway.accounts import CreditsRefresher
        refresher = CreditsRefresher(_state.POOL)
        refresher.start()

    httpd = build_server(args.host, args.port)
    display = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    log("wb2api gateway on http://%s:%d  (realm=%s, accounts=%d/%d ready, "
        "api key %s, tasks %s)"
        % (display, args.port, realm,
           _state.POOL.count_ready(), len(_state.POOL.accounts),
           "on" if settings.get_api_key(_state.ACCOUNTS_DIR) else "OFF (open locally)",
           "on" if args.tasks else "off"))
    log("OpenAI base URL: http://%s:%d/v1" % (display, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception:
                pass
        if refresher is not None:
            try:
                refresher.stop()
            except Exception:
                pass
    return 0
