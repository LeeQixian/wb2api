# -*- coding: utf-8 -*-
"""Offline test helper: hand-built base64url JWTs, never real tokens."""

import base64
import json


def b64url(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_jwt(sub="user-1234", iss="https://www.workbuddy.ai", exp=None, **extra):
    """Build a JWT-shaped string; only the payload segment is ever decoded."""
    if exp is None:
        import time
        exp = int(time.time()) + 3600
    claims = {"sub": sub, "iss": iss, "exp": exp}
    claims.update(extra)
    return b64url({"alg": "HS256", "typ": "JWT"}) + "." + b64url(claims) + "." + "sig"
