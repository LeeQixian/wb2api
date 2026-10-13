"""wbctl - command-line account & key management (replaces the dashboard).

    python3 -m gateway.wbctl [--dir accounts] <command> ...

  login [--realm intl|cn]          browser OAuth device flow, waits and saves
  import <token|file|-> [--refresh <rt>] [--realm r] [--nickname n]
      accepts a raw accessToken JWT, a desktop .info file (nested
      account/auth shape), or a hub accounts/*.json file (flat)
  list                             one line per account
  remove <uid> | enable <uid> | disable <uid>
  refresh <uid>                    force a token refresh now
  credits [uid]                    query balance/packages (all if uid omitted)
  checkin <uid>                    CN realm daily checkin
  daily-chat <uid>                 intl daily activity chat (web-channel per setting)
  growthplan <uid>                 one-shot growth tasks (not run by the scheduler)
  realm [intl|cn]                  read/switch the persisted global exit
  product <uid> <workbuddy|vscode|cli>   pin the outbound identity (persisted)
  set-switch <name> <value>        reserve-credits | daily-token-limit |
                                   auto-switch-product | daily-chat-web |
                                   local-web-tools | expiring-window | credits-refresh
  set-prompt [mode] [--file f]     gateway system-prompt mode (passthrough/custom/append)
  sync-nicknames                   fill missing account nicknames from the console
  set-key [<key>] | clear-key      gateway API key (auth off when cleared)

Exit code non-zero + a one-line stderr message on failure.
"""
import argparse
import getpass
import os
import sys
import time

from gateway import accounts as accounts_mod
from gateway import settings
from gateway.accounts import AccountPool

DEFAULT_DIR = os.environ.get("WB_ACCOUNTS_DIR") or \
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "accounts")


def _pool(args):
    pool = AccountPool(args.dir, log=lambda msg: print(msg, file=sys.stderr))
    pool.load()
    return pool


def cmd_login(args):
    pool = _pool(args)
    info = pool.start_login(realm=args.realm)
    print("Open this URL in a browser and finish the login:\n")
    print("  " + info["authUrl"])
    print("\nWaiting up to %ds..." % 600)
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(3)
        res = pool.poll_login(info["state"])
        status = res.get("status")
        if status == "ok":
            acc = res.get("account") or {}
            print("added: uid=%s realm=%s nickname=%s"
                  % (acc.get("uid"), acc.get("realm"), acc.get("nickname")))
            return 0
        if status in ("error", "expired", "unknown"):
            print("login failed: %s" % res.get("message"), file=sys.stderr)
            return 1
        print(".", end="", flush=True)
    print("\nlogin timed out", file=sys.stderr)
    return 1


def cmd_import(args):
    pool = _pool(args)
    blob = args.source
    if blob == "-":
        blob = sys.stdin.read().strip()
    elif os.path.isfile(blob):
        # credential file: desktop .info, hub accounts/*.json, or a bare token
        with open(blob, encoding="utf-8") as fh:
            blob = fh.read().strip()
    account = pool.import_credential(blob, realm=args.realm)
    if args.refresh:
        account.refresh_token = args.refresh
        account.save(pool.dir)
    if args.nickname:
        account.nickname = args.nickname
        account.save(pool.dir)
    print("saved: uid=%s realm=%s nickname=%s expires=%s file=%s"
          % (account.uid, account.realm, account.nickname,
             time.strftime("%Y-%m-%d %H:%M", time.localtime(account.expires_at))
             if account.expires_at else "?",
             os.path.basename(account.path or "")))
    return 0


def cmd_list(args):
    pool = _pool(args)
    if not pool.accounts:
        print("no accounts in %s - add one with: wbctl login / wbctl import" % pool.dir)
        return 0
    for a in pool.accounts:
        p = a.public()
        credits = "-"
        if isinstance(p.get("credits"), dict):
            credits = "%s" % p["credits"].get("remain", "-")
        print("%-22s %-5s %-12s %-4s ready=%-3s exp=%-12s credits=%s %s"
              % (p["uid"][:22], p["realm"], (p["nickname"] or "")[:12],
                 "on" if p["enabled"] else "off",
                 "y" if a.ready(allow_refresh=False) else "n",
                 p.get("expiresIn") or "-",
                 credits,
                 ("err: " + p["lastError"][:40]) if p.get("lastError") else ""))
    return 0


def _one(args, pool):
    account, err = pool.resolve(args.uid)
    if account is None:
        print(err, file=sys.stderr)
        return None
    return account


def cmd_remove(args):
    pool = _pool(args)
    account, err = pool.resolve(args.uid)
    if account is None:
        print(err, file=sys.stderr)
        return 1
    return 0 if pool.remove(account.uid) else 1


def cmd_toggle(enabled):
    def run(args):
        pool = _pool(args)
        account, err = pool.resolve(args.uid)
        if account is None:
            print(err, file=sys.stderr)
            return 1
        pool.set_enabled(account.uid, enabled)
        print("%s %s" % ("enabled" if enabled else "disabled", account.uid[:8]))
        return 0
    return run


def cmd_refresh(args):
    pool = _pool(args)
    account = _one(args, pool)
    if account is None:
        return 1
    ok = account.refresh()
    if ok:
        account.save(pool.dir)
        print("refreshed: expires=%s" % account.expires_at)
    else:
        print("refresh failed: %s" % account.last_error, file=sys.stderr)
    return 0 if ok else 1


def cmd_credits(args):
    pool = _pool(args)
    targets = [pool.get(args.uid)] if args.uid else list(pool.accounts)
    rc = 0
    for account in targets:
        if account is None:
            print("no such uid: %s" % args.uid, file=sys.stderr)
            return 1
        res = account.fetch_credits()
        if not res.get("ok"):
            print("%s: error %s" % (account.uid, res.get("error")), file=sys.stderr)
            rc = 1
            continue
        c = res["credits"]
        print("%s [%s] remain=%s used=%s size=%s" %
              (account.uid, account.realm, c["remain"], c["used"], c["size"]))
        for pkg in c.get("packages") or []:
            print("    %-24s remain=%s/%s" % (pkg["name"][:24], pkg["remain"], pkg["size"]))
    return rc


def cmd_checkin(args):
    pool = _pool(args)
    account = _one(args, pool)
    if account is None:
        return 1
    res = account.checkin()
    print(res)
    return 0 if res.get("ok") else 1


def cmd_daily_chat(args):
    pool = _pool(args)
    account = _one(args, pool)
    if account is None:
        return 1
    res = account.daily_chat()
    print(res)
    return 0 if res.get("ok") else 1


def cmd_growthplan(args):
    """Run the one-shot growth tasks (accept/claim/auto) for one account."""
    from gateway import tasks as wb_tasks
    pool = _pool(args)
    account = _one(args, pool)
    if account is None:
        return 1
    res = wb_tasks.run_growth_tasks(account)
    for line in res.get("logs", []):
        print(line)
    print("earned=%s" % res.get("earned"))
    return 0 if res.get("ok") else 1


def cmd_realm(args):
    """Read or set the persisted global exit (the POST /realm CLI twin)."""
    from gateway import upstream
    if args.name is None:
        print(upstream.load_persisted_realm(args.dir))
        return 0
    upstream.save_persisted_realm(args.dir, args.name)
    print("realm = %s (persisted; a running gateway picks it up on next start,"
          " or POST /realm while live)" % args.name)
    return 0

def cmd_product(args):
    pool = _pool(args)
    account = _one(args, pool)
    if account is None:
        return 1
    if not account.set_product(args.name):
        print("already on identity: %s" % account.product)
    # Persist now: this is an explicit choice, not a side effect of a later save.
    account.save(pool.dir)
    print("product=%s endpoint=%s (persisted)" % (account.product, account.chat_base_url()))
    return 0


_SWITCHES = {
    "reserve-credits": (settings.set_reserve_credits, settings.reserve_credits),
    "daily-token-limit": (settings.set_daily_token_limit, settings.daily_token_limit),
    "auto-switch-product": (settings.set_auto_switch_product, settings.auto_switch_product),
    "daily-chat-web": (settings.set_daily_chat_web, settings.daily_chat_web),
    "local-web-tools": (settings.set_local_web_tools, settings.local_web_tools),
    "expiring-window": (settings.set_expiring_window_days, settings.expiring_window_days),
    "credits-refresh": (settings.set_credits_refresh_hours, settings.credits_refresh_hours),
}


def _as_bool(value):
    v = str(value or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError("expected a boolean (on/off/true/false)")


def cmd_set_switch(args):
    if args.name not in _SWITCHES:
        print("unknown switch: %s (pick one of %s)" % (args.name, ", ".join(sorted(_SWITCHES))),
              file=sys.stderr)
        return 1
    setter, getter = _SWITCHES[args.name]
    if args.value is None:
        print("%s = %r" % (args.name, getter(args.dir)))
        return 0
    raw = args.value
    if args.name in ("auto-switch-product", "daily-chat-web", "local-web-tools"):
        raw = _as_bool(raw)
    setter(args.dir, raw)
    print("%s = %r" % (args.name, getter(args.dir)))
    return 0


def cmd_set_prompt(args):
    if args.mode is None and args.file is None:
        print(settings.prompt_config(args.dir))
        return 0
    settings.set_prompt_config(args.dir, mode=args.mode, file=args.file)
    print("prompt = %r" % (settings.prompt_config(args.dir),))
    return 0


def cmd_sync_nicknames(args):
    pool = _pool(args)
    rc = 0
    for account in pool.accounts:
        if account.nickname and not account.nickname.startswith(account.uid[:6]):
            print("%s: nickname already set (%s)" % (account.uid[:8], account.nickname))
            continue
        try:
            _uid, nickname = accounts_mod.fetch_account_profile(account)
        except Exception as exc:
            print("%s: profile fetch failed: %s" % (account.uid[:8], exc),
                  file=sys.stderr)
            rc = 1
            continue
        if not nickname:
            print("%s: profile carries no nickname" % account.uid[:8])
            continue
        account.nickname = nickname
        account.save(pool.dir)
        print("%s: nickname -> %s (persisted)" % (account.uid[:8], nickname))
    return rc


def cmd_set_key(args):
    key = args.key
    if not key or key == "-":
        key = sys.stdin.readline().strip() if key == "-" else getpass.getpass("API key: ")
    if not key:
        print("empty key refused; use clear-key to disable auth", file=sys.stderr)
        return 1
    settings.set_api_key(args.dir, key)
    print("api key saved to %s (auth is now enforced on inference routes)"
          % os.path.join(args.dir, "settings.json"))
    return 0


def cmd_clear_key(args):
    settings.clear_api_key(args.dir)
    print("api key cleared (all local requests pass)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wbctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="accounts directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="browser OAuth, waits and saves")
    p.add_argument("--realm", choices=("intl", "cn"), default="intl")
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("import", help="register from a JWT / credential file / '-'")
    p.add_argument("source", help="accessToken JWT, path to .info or accounts/*.json, or '-'")
    p.add_argument("--refresh", default=None)
    p.add_argument("--realm", choices=("intl", "cn"), default=None)
    p.add_argument("--nickname", default=None)
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("list", help="show accounts")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("realm", help="read/switch the persisted global exit")
    p.add_argument("name", nargs="?", choices=("intl", "cn"), default=None)
    p.set_defaults(fn=cmd_realm)

    for name, fn in (("remove", cmd_remove),
                     ("enable", cmd_toggle(True)),
                     ("disable", cmd_toggle(False)),
                     ("refresh", cmd_refresh),
                     ("checkin", cmd_checkin),
                     ("daily-chat", cmd_daily_chat)):
        p = sub.add_parser(name)
        p.add_argument("uid")
        p.set_defaults(fn=fn)


    p = sub.add_parser("product", help="pin the outbound identity (persisted)")
    p.add_argument("uid")
    p.add_argument("name", choices=("workbuddy", "vscode", "cli", "wb", "desktop", "vsc", "ide", "codebuddy"))
    p.set_defaults(fn=cmd_product)

    p = sub.add_parser("set-switch", help="read/write a settings.json guard switch")
    p.add_argument("name", choices=tuple(sorted(_SWITCHES)))
    p.add_argument("value", nargs="?", default=None)
    p.set_defaults(fn=cmd_set_switch)

    p = sub.add_parser("credits", help="query balances")
    p.add_argument("uid", nargs="?")
    p.set_defaults(fn=cmd_credits)

    p = sub.add_parser("growthplan",
                       help="run one-shot growth tasks for one account")
    p.add_argument("uid")
    p.set_defaults(fn=cmd_growthplan)

    p = sub.add_parser("set-prompt",
                       help="read/set the gateway system-prompt mode (#160)")
    p.add_argument("mode", nargs="?", choices=("passthrough", "custom", "append"),
                   default=None)
    p.add_argument("--file", default=None, help="prompt text file for custom/append")
    p.set_defaults(fn=cmd_set_prompt)

    p = sub.add_parser("sync-nicknames",
                       help="fill missing nicknames from the web console (#158)")
    p.set_defaults(fn=cmd_sync_nicknames)

    p = sub.add_parser("set-key", help="persist the gateway API key")
    p.add_argument("key", nargs="?", default=None)
    p.set_defaults(fn=cmd_set_key)

    p = sub.add_parser("clear-key", help="remove the API key (auth off)")
    p.set_defaults(fn=cmd_clear_key)

    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
