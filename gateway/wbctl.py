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
  set-key [<key>] | clear-key      gateway API key (auth off when cleared)

Exit code non-zero + a one-line stderr message on failure.
"""
import argparse
import getpass
import os
import sys
import time

from gateway import settings
from gateway.accounts import AccountPool

DEFAULT_DIR = os.environ.get("WB_ACCOUNTS_DIR") or \
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "accounts")


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
                 "y" if a.ready() else "n",
                 p.get("expiresIn") or "-",
                 credits,
                 ("err: " + p["lastError"][:40]) if p.get("lastError") else ""))
    return 0


def _one(args, pool):
    account = pool.get(args.uid)
    if account is None:
        print("no such uid: %s" % args.uid, file=sys.stderr)
        return None
    return account


def cmd_remove(args):
    pool = _pool(args)
    return 0 if pool.remove(args.uid) else (print("no such uid", file=sys.stderr) or 1)


def cmd_toggle(enabled):
    def run(args):
        pool = _pool(args)
        return 0 if pool.set_enabled(args.uid, enabled) else (
            print("no such uid", file=sys.stderr) or 1)
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

    for name, fn in (("remove", cmd_remove),
                     ("enable", cmd_toggle(True)),
                     ("disable", cmd_toggle(False)),
                     ("refresh", cmd_refresh),
                     ("checkin", cmd_checkin)):
        p = sub.add_parser(name)
        p.add_argument("uid")
        p.set_defaults(fn=fn)

    p = sub.add_parser("credits", help="query balances")
    p.add_argument("uid", nargs="?")
    p.set_defaults(fn=cmd_credits)

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
