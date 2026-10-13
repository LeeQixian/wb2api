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
        refresh_pool()
        info = {
            "ok": True,
            "realm": upstream.get_realm(),
            "accounts": len(_state.POOL.accounts) if _state.POOL else 0,
            "accounts_ready": _state.POOL.count_ready() if _state.POOL else 0,
            "api_key_required": bool(settings.get_api_key(_state.ACCOUNTS_DIR)),
        }
        rep = _state.POOL.representative() if _state.POOL else None
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
                # explicit single-exit probe (X-Realm / ?realm=) keeps the
                # narrow view of exactly that exit's catalog
                entries = upstream.fetch_models(realm=req_realm, pool=pool)
                data = [upstream.model_entry(mid, meta) for mid, meta in entries]
                return self._json(200, {"object": "list", "data": data,
                                        "realm": req_realm})
            # no explicit realm: the full menu across BOTH exits with
            # cn:/global: exit aliases, independent of the global default
            # (upstream.fetch_models_all; server side pairs with
            #  translate.split_realm_prefix)
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
