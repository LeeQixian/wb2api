"""GET routes: /health, /realm, /v1/models, /credits."""

import json
import os
import urllib.error
from urllib.parse import urlparse

from gateway import settings, translate, upstream
from gateway.common.log import log
from gateway.http import state as _state
from gateway.http.state import refresh_pool


class GetRoutes(object):
    """GET handlers mixed into the request handler."""

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._get_health()
        if path == "/realm":
            return self._json(200, {"current": upstream.get_realm(),
                                    "options": ["intl", "cn"]})
        if path in ("/v1/models", "/models"):
            return self._get_v1_models()
        if path in ("/credits", "/v1/credits"):
            return self._get_credits()
        return self._error(404, "not found", "invalid_request_error")


    def _get_health(self):
        pool = refresh_pool()
        realm = upstream.get_realm()
        accounts = list(pool.accounts) if pool else []
        realms = []
        for account in accounts:
            if account.realm not in realms:
                realms.append(account.realm)
        info = {
            "ok": True,
            "realm": realm,
            "accounts": len(accounts),
            "accounts_ready": pool.count_ready() if pool else 0,
            "accounts_by_realm": {r: len([a for a in accounts if a.realm == r])
                                  for r in realms},
            "accounts_ready_by_realm": {r: pool.count_ready(realm=r) for r in realms},
            "accounts_ready_current_realm": pool.count_ready(realm=realm) if pool else 0,
            "api_key_required": bool(settings.get_api_key(_state.ACCOUNTS_DIR)),
        }
        # Only per-model cooldowns are known locally; the counts stay read-only.
        models = sorted({model for a in accounts
                         for model in (getattr(a, "model_cooldowns", None) or {})})[:50]
        if models:
            info["models_ready"] = {
                model: {r: pool.count_ready(realm=r, model=model) for r in realms}
                for model in models}
        rep = pool.representative() if pool else None
        if rep is not None:
            info.update({
                "uid": rep.uid,
                "expires_at": getattr(rep, "expires_at", None),
                "credential_file": os.path.basename(rep.path or "") if rep.path else None,
            })
        return self._json(200, info)


    def _get_v1_models(self):
        pool = refresh_pool()
        req_realm = self._request_realm()
        try:
            if req_realm in ("intl", "cn"):
                # An explicit single-exit probe (X-Realm / ?realm=) keeps the
                # narrow view of exactly that exit's catalog.
                entries = upstream.fetch_models(realm=req_realm, pool=pool)
                data = [upstream.model_entry(mid, meta) for mid, meta in entries]
                return self._json(200, {"object": "list", "data": data,
                                        "realm": req_realm})
            # No explicit realm: list both exits with cn:/global: aliases,
            # independent of the global default.
            entries = upstream.fetch_models_all(pool=pool)
            data = [upstream.model_entry(mid, meta) for mid, meta in entries]
            return self._json(200, {"object": "list", "data": data, "realm": "all"})
        except Exception as exc:
            return self._error(502, str(exc))


    def _get_credits(self):
        refresh_pool()
        out = []
        for a in list(_state.POOL.accounts):
            row = {"uid": a.uid, "realm": a.realm,
                   "nickname": getattr(a, "nickname", "") or "",
                   "enabled": a.enabled,
                   "expires_at": getattr(a, "expires_at", None)}
            try:
                res = a.fetch_credits()
                if isinstance(res, dict):
                    row["credits"] = res
            except Exception as exc:
                row["credits_error"] = str(exc)[:160]
            out.append(row)
        return self._json(200, {"accounts": out})
