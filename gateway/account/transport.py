"""HTTP transport with retry, proxy support and keep-alive pool fallback."""

import json
import time
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

from gateway.account import jwt as _jwt
from gateway.common.log import log


def _upstream_pool():
    """Late import: upstream_pool imports accounts for proxy helpers."""
    from gateway.upstream import pool as upstream_pool
    return upstream_pool


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
    from gateway.upstream.retry import is_tls_verify_error
    if is_tls_verify_error(exc):
        return False
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()


def opener_for_proxy(proxy):
    """Cached urllib opener bound to one proxy; None means the default one.
    One opener per account pins each account to its own exit IP."""
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            _OPENER_CACHE[proxy] = opener
        return opener


def urlopen(req, timeout=30, proxy=""):
    """urlopen over a keep-alive pool; PoolBypass falls back to plain urllib."""
    from gateway.upstream import pool as upstream_pool
    if upstream_pool.enabled():
        try:
            return upstream_pool.urlopen(req, timeout=timeout, proxy=proxy)
        except upstream_pool.PoolBypass:
            pass
    opener = opener_for_proxy(proxy)
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None, proxy=""):
    """urlopen + JSON decode with retries; all upstream calls go through here.
    CDN edges routinely drop TLS handshakes, and one retry fixes them."""
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=headers or {},
        )
        try:
            with urlopen(req, timeout=timeout, proxy=proxy) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last
