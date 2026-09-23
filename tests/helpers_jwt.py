# -*- coding: utf-8 -*-
"""离线测试辅助：自造 base64url JWT（不用真实 token 值）。"""

import base64
import json


def b64url(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_jwt(sub="user-1234", iss="https://www.workbuddy.ai", exp=None, **extra):
    """造一个形似 JWT 的字符串；签名段没有意义，本仓库只解 payload。"""
    if exp is None:
        import time
        exp = int(time.time()) + 3600
    claims = {"sub": sub, "iss": iss, "exp": exp}
    claims.update(extra)
    return b64url({"alg": "HS256", "typ": "JWT"}) + "." + b64url(claims) + "." + "sig"
