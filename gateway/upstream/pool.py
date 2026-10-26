

import base64
import hashlib
import http.client
import io
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


# -*- coding: utf-8 -*-
"""Keep-alive HTTP pool keyed by (scheme, host, port, proxy, credential)."""

# Reuses http.client connections instead of urllib's do_open, which hardcodes
# Connection: close and pays a fresh TCP+TLS handshake per request.

# PoolBypass means callers fall back to urllib: non-http(s) URLs, non-HTTP
# proxies, and any 3xx response (redirect handling stays in urllib).

# WB_UPSTREAM_KEEPALIVE=0 disables the pool; WB_UPSTREAM_POOL_SIZE and
# WB_UPSTREAM_POOL_IDLE size it. Stale connections are swept on every reuse.

# Two accounts never share one TCP/TLS connection: the credential digest is
# the last key dimension.

#: Values of WB_UPSTREAM_KEEPALIVE that disable the pool; anything else is on.
_KEEPALIVE_OFF = ("0", "off", "false", "no")

#: Cap on error-body reads; larger bodies are not read and the connection dropped.
_ERROR_BODY_MAX = 1024 * 1024

_SSL_CONTEXT = None
_SSL_CONTEXT_KEY = None
_SSL_CONTEXT_LOCK = threading.Lock()


def ca_bundle_path():
    """PEM bundle for a private TLS chain; WB_CA_BUNDLE wins over SSL_CERT_FILE.
    A set-but-missing WB_CA_BUNDLE is a config error, never a silent fallback."""
    for name in ("WB_CA_BUNDLE", "SSL_CERT_FILE"):
        raw = os.environ.get(name)
        if not raw or not str(raw).strip():
            continue
        path = os.path.abspath(os.path.expanduser(str(raw).strip()))
        if os.path.isfile(path):
            return path
        if name == "WB_CA_BUNDLE":
            raise RuntimeError("%s points to a missing file: %s" % (name, path))
        # OpenSSL ignores a missing SSL_CERT_FILE, so that case stays harmless.
    return ""


# The pool builds its own context, but urllib still serves fallback paths;
# exporting through the variable OpenSSL understands keeps both trust sets.
if os.environ.get("WB_CA_BUNDLE"):
    _ca = ca_bundle_path()
    if _ca:
        os.environ["SSL_CERT_FILE"] = _ca


def _resolve_ssl_factory():
    """(identity, builder) of the context http.client would make by default.
    Tracking the seam lets tests swap the default and invalidate the cache."""
    factory = getattr(http.client, "_create_https_context", None)
    if factory is not None:
        return factory, (lambda: factory(None))
    factory = getattr(ssl, "_create_default_https_context", None)
    if factory is not None:
        return factory, factory
    return ssl.create_default_context, ssl.create_default_context


def shared_ssl_context():
    """Process-wide TLS context: per-connection rebuild costs ~20ms and drops
    session resumption; verification settings stay exactly stdlib's defaults."""
    global _SSL_CONTEXT, _SSL_CONTEXT_KEY
    ident, build = _resolve_ssl_factory()
    key = (ident, ca_bundle_path())
    if _SSL_CONTEXT is None or _SSL_CONTEXT_KEY != key:
        with _SSL_CONTEXT_LOCK:
            ident, build = _resolve_ssl_factory()
            key = (ident, ca_bundle_path())
            if _SSL_CONTEXT is None or _SSL_CONTEXT_KEY != key:
                if key[1]:
                    # Keep system roots and add the private bundle on top.
                    _SSL_CONTEXT = ssl.create_default_context()
                    _SSL_CONTEXT.load_verify_locations(cafile=key[1])
                else:
                    _SSL_CONTEXT = build()
                _SSL_CONTEXT_KEY = key
    return _SSL_CONTEXT


def enabled():
    """True unless WB_UPSTREAM_KEEPALIVE is 0/off/false/no; read per call."""
    raw = os.environ.get("WB_UPSTREAM_KEEPALIVE")
    if raw is None:
        return True
    return str(raw).strip().lower() not in _KEEPALIVE_OFF


def _pool_size():
    """Idle connections kept per pool key (WB_UPSTREAM_POOL_SIZE, default 2)."""
    try:
        return max(1, int(os.environ.get("WB_UPSTREAM_POOL_SIZE") or "2"))
    except (TypeError, ValueError):
        return 2


def _idle_ttl():
    """Max idle seconds before a connection is dropped (default 90)."""
    try:
        return max(5.0, float(os.environ.get("WB_UPSTREAM_POOL_IDLE") or "90"))
    except (TypeError, ValueError):
        return 90.0


class PoolBypass(Exception):
    """This request must not pool; the caller falls back to urllib."""


class _StaleConnection(Exception):
    """A pooled connection closed by the peer before any byte arrived."""

    def __init__(self, original):
        Exception.__init__(self, str(original))
        self.original = original


class _TransportFailure(Exception):
    """Transport failure; the connection is unusable. `original` is re-raised."""

    def __init__(self, original):
        Exception.__init__(self, str(original))
        self.original = original


def _stale_connection_error(exc):
    """True for connection-level failures: write errors or EOF before any byte."""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(exc, ssl.SSLEOFError):
        return True
    # Some OpenSSL builds report a missing close_notify as a plain SSLError
    # carrying UNEXPECTED_EOF; treat it like SSLEOFError.
    if isinstance(exc, ssl.SSLError) and "UNEXPECTED_EOF" in str(exc).upper():
        return True
    return False


def _connection_looks_alive(conn):
    """True if an idle connection is still clean and reusable.
    Probe with non-blocking recv(1): readable bytes or errors mean discard."""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    pending = getattr(sock, "pending", None)
    if pending is not None:
        try:
            if pending() > 0:
                return False
        except Exception:
            return False
    try:
        sock.settimeout(0)
        sock.recv(1)
    except (BlockingIOError, ssl.SSLWantReadError):
        return True
    except Exception:
        return False
    return False


class _PooledResponse(http.client.HTTPResponse):
    """HTTPResponse that returns its connection to the pool when done.
    Release follows natural completion, not isclosed(); mid-stream closes drop."""
    _pool = None          # the next three are set by _exchange() after getresponse()
    _pool_key = None
    _pool_conn = None
    _released = False
    _chunked_done = False

    def _read_and_discard_trailer(self):
        # Record "chunked reached its end" before http.client closes the
        # connection, so _release() can trust it.
        http.client.HTTPResponse._read_and_discard_trailer(self)
        self._chunked_done = True

    def _body_complete(self):
        """True if the body was read to its natural end."""
        if not self.isclosed():
            return False
        if self.chunked:
            return self._chunked_done
        # Non-chunked: length hits 0 only at the end; a mid-stream close leaves it > 0.
        return self.length == 0

    def _release(self):
        """Return the connection to the pool or close it; idempotent."""
        if self._released:
            return
        self._released = True
        pool, key, conn = self._pool, self._pool_key, self._pool_conn
        if pool is None or conn is None:
            return
        if (self._body_complete() and not self.will_close
                and conn.sock is not None):
            pool.release(key, conn)
        else:
            # will_close, a partial body or a dead socket means discard: a
            # reused dirty connection would leak the previous body.
            pool.discard(conn)

    def _close_conn(self):
        http.client.HTTPResponse._close_conn(self)
        self._release()

    def close(self):
        http.client.HTTPResponse.close(self)
        self._release()


class _PooledHTTPConnection(http.client.HTTPConnection):
    response_class = _PooledResponse


class _PooledHTTPSConnection(http.client.HTTPSConnection):
    response_class = _PooledResponse


class _KeepAlivePool(object):
    """Idle connections by key. The lock guards _idle only; one owner per conn."""

    def __init__(self):
        self._lock = threading.Lock()
        # key -> [(conn, last-release monotonic), ...]; oldest first, newest last.
        self._idle = {}
        # Diagnostics: `created` counts real new connections, `reused` counts
        # pool hits (direct evidence that per-request SYNs are gone).
        self.stats = {"created": 0, "reused": 0, "evicted": 0,
                      "dead": 0, "discarded": 0}

    def acquire(self, key, timeout, factory):
        """Return a live pooled connection (reused=True) or factory() a new one."""
        conn = None
        now = time.monotonic()
        ttl = _idle_ttl()
        with self._lock:
            self._sweep_locked(now)
            entries = self._idle.get(key)
            while entries:
                cand, ts = entries.pop()      # LIFO: reuse the most recently used
                if now - ts <= ttl:
                    conn = cand
                    break
                # Past idle TTL: the upstream likely closed it. Keep the check
                # as defense; the sweep runs globally anyway.
                self.stats["evicted"] += 1
                self._close(cand)
            if entries is not None and not entries:
                self._idle.pop(key, None)
        if conn is not None:
            if not _connection_looks_alive(conn):
                self.stats["dead"] += 1
                self._close(conn)
                conn = None
            else:
                # Reset the read timeout for this request: the previous owner
                # may have left the idle-timeout value on the socket.
                try:
                    conn.sock.settimeout(timeout)
                except Exception:
                    self._close(conn)
                    conn = None
        if conn is None:
            conn = factory()
            self.stats["created"] += 1
            return conn, False
        self.stats["reused"] += 1
        return conn, True

    def release(self, key, conn):
        """Return a drained connection; evict the oldest one when the pool is full."""
        cap = _pool_size()
        with self._lock:
            self._sweep_locked(time.monotonic())
            entries = self._idle.setdefault(key, [])
            while len(entries) >= cap:
                oldest, _ts = entries.pop(0)
                self.stats["evicted"] += 1
                self._close(oldest)
            entries.append((conn, time.monotonic()))

    def _sweep_locked(self, now):
        """Close idle connections past their TTL across every key.
        Sweep all keys, or an orphan socket lives until exit. Caller holds _lock."""
        ttl = _idle_ttl()
        for key in list(self._idle):
            alive = []
            for conn, ts in self._idle[key]:
                if now - ts <= ttl:
                    alive.append((conn, ts))
                else:
                    self.stats["evicted"] += 1
                    self._close(conn)
            if alive:
                self._idle[key] = alive
            else:
                self._idle.pop(key, None)

    def discard(self, conn):
        """Drop this connection entirely; it is no longer trustworthy."""
        self.stats["discarded"] += 1
        self._close(conn)

    def note_created(self):
        """Count a new connection. acquire() logs its misses already; retry paths
        that call factory() directly must call this too, or the count drops them."""
        with self._lock:
            self.stats["created"] += 1

    def _close(self, conn):
        try:
            conn.close()
        except Exception:
            pass

    # ---- tests and diagnostics ----

    def idle_counts(self):
        """Current idle connection count per key (for capacity tests)."""
        with self._lock:
            return {k: len(v) for k, v in self._idle.items() if v}

    def close_all(self):
        """Close every idle connection (test cleanup)."""
        with self._lock:
            conns = [c for entries in self._idle.values() for c, _ in entries]
            self._idle.clear()
        for conn in conns:
            self._close(conn)


_POOL = _KeepAlivePool()


def stats():
    """Cumulative pool counters."""
    return dict(_POOL.stats)


def idle_counts():
    """Idle connection counts per key."""
    return _POOL.idle_counts()


def close_all():
    """Close every idle connection in the pool."""
    _POOL.close_all()


# --- Request orchestration: urllib.Request -> one http.client exchange. -----

def _target_parts(url):
    """Split URL into (scheme, host, port, selector); selector matches urllib."""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise PoolBypass("unsupported url for the keep-alive pool: %r" % (url,))
    port = parts.port or (443 if scheme == "https" else 80)
    selector = parts.path or "/"
    if parts.query:
        selector += "?" + parts.query
    return scheme, parts.hostname, port, selector


def _split_proxy(proxy):
    """Parse a proxy string into dict(scheme, user, password, host, port).
    None for non-HTTP proxies (socks5://), which urllib then handles."""
    raw = str(proxy or "").strip()
    if not raw:
        return None
    if "://" in raw:
        scheme, _, rest = raw.partition("://")
        if scheme.strip().lower() != "http":
            return None
        authority = rest.split("/", 1)[0]
    else:
        authority = raw                      # authority form: host:port
    user = password = None
    if "@" in authority:
        userinfo, _, authority = authority.rpartition("@")
        user, _, password = userinfo.partition(":")
        user = urllib.parse.unquote(user)
        password = urllib.parse.unquote(password) if password else password
    hostport = urllib.parse.unquote(authority)
    if hostport.startswith("["):             # IPv6: [::1]:8080 / [::1]
        host, sep, rest = hostport[1:].partition("]")
        port = rest.lstrip(":") if sep else ""
    elif ":" in hostport:
        host, _, port = hostport.rpartition(":")
    else:
        host, port = hostport, ""
    if not host:
        return None
    if port and not port.isdigit():
        # A non-numeric port makes urllib's HTTPConnection raise InvalidURL;
        # treat it as unpoolable and fall back with the same error shape.
        return None
    return {"scheme": "http", "user": user or "", "password": password or "",
            "host": host, "port": port}


def _proxy_auth_header(proxy_info):
    """Proxy-Authorization value, or "" when there are no credentials.
    Sent only when user and password are non-empty after URL-decoding."""
    if not proxy_info or not proxy_info.get("user") or not proxy_info.get("password"):
        return ""
    creds = "%s:%s" % (proxy_info["user"], proxy_info["password"])
    return "Basic " + base64.b64encode(creds.encode()).decode("ascii")


def _proxy_bypassed(host):
    """True when the host is on the system proxy bypass list.
    Mirrors urllib proxy_bypass (Windows ProxyOverride, Linux no_proxy)."""
    try:
        return bool(urllib.request.proxy_bypass(host))
    except Exception:
        return False


def _new_connection(scheme, host, port, proxy_info, timeout):
    """Build an unconnected client; http.client connects lazily.
    Direct uses HTTPSConnection, proxies use CONNECT (HTTPS) or absolute URLs."""
    if proxy_info:
        # Default ports follow urllib: 443 for HTTPS, 80 for HTTP when the
        # proxy string omits one.
        proxy_port = int(proxy_info["port"]) if proxy_info["port"] else (
            443 if scheme == "https" else 80)
        if scheme == "https":
            conn = _PooledHTTPSConnection(proxy_info["host"], proxy_port,
                                          timeout=timeout,
                                          context=shared_ssl_context())
            tunnel_headers = {}
            auth = _proxy_auth_header(proxy_info)
            if auth:
                # Proxy-Authorization is proxy-only; never forward it to the origin.
                tunnel_headers["Proxy-Authorization"] = auth
            conn.set_tunnel(host, port, headers=tunnel_headers)
            return conn
        return _PooledHTTPConnection(proxy_info["host"], proxy_port, timeout=timeout)
    if scheme == "https":
        return _PooledHTTPSConnection(host, port, timeout=timeout,
                                      context=shared_ssl_context())
    return _PooledHTTPConnection(host, port, timeout=timeout)


def _host_header_value(url):
    """Host header value: the URL netloc, explicit port included (as urllib)."""
    netloc = urllib.parse.urlsplit(url).netloc
    if "@" in netloc:
        netloc = netloc.rpartition("@")[2]
    return netloc


#: Pool-key identity fields; the first present one isolates accounts.
_IDENTITY_HEADER_FIELDS = ("authorization", "x-refresh-token", "x-user-id")


def _header_lookup(pairs):
    """Fold (name, value) pairs into {lower name: first value}."""
    lookup = {}
    for name, value in pairs:
        key = str(name).strip().lower()
        if key not in lookup and value:
            lookup[key] = value
    return lookup


def _identity_discriminator(req, ordered_headers=None):
    """Credential digest for the pool key; '' when the request has no credential.
    Two accounts on one exit must never share a connection."""
    pairs = ordered_headers if ordered_headers is not None else list(req.headers.items())
    lookup = _header_lookup(pairs)
    for field in _IDENTITY_HEADER_FIELDS:
        value = lookup.get(field)
        if value:
            digest = hashlib.sha256(
                ("%s\0%s" % (field, value)).encode("utf-8")).hexdigest()[:16]
            return "%s:%s" % (field, digest)
    return ""


def _exact_header_pairs(headers):
    """Normalize caller-supplied ordered headers to [(name, value)]; None keeps default."""
    if headers is None:
        return None
    if hasattr(headers, "items"):
        return list(headers.items())
    return list(headers)


def _request_headers(req, scheme, proxy_info, ordered_headers=None):
    """Wire headers as urllib do_open builds them, plus Connection: keep-alive.
    ordered_headers: send those names verbatim (no .title()); chat path only."""
    headers = dict(req.unredirected_hdrs)
    data = req.data
    ordered_names = None
    if ordered_headers is not None:
        ordered_names = {str(name).strip().lower() for name, _value in ordered_headers}
    if data is not None:
        # do_request_ default: add a form Content-type when a body lacks one.
        if not req.has_header("Content-type") and not (
                ordered_names is not None and "content-type" in ordered_names):
            headers["Content-type"] = "application/x-www-form-urlencoded"
        # Content-Length is left to http.client._send_request, which places it
        # exactly where urllib would.
    if not req.has_header("Host") and not (
            ordered_names is not None and "host" in ordered_names):
        headers["Host"] = _host_header_value(req.full_url)
    if not req.has_header("User-agent") and not (
            ordered_names is not None and "user-agent" in ordered_names):
        # Fallback UA for the default opener; every caller passes one explicitly.
        headers["User-agent"] = "Python-urllib/%s" % urllib.request.__version__
    if ordered_headers is None:
        headers.update(req.headers)
    else:
        for name, value in ordered_headers:
            headers[name] = value
    if proxy_info and scheme == "http":
        # HTTP via proxy: auth rides the request; HTTPS carries it in CONNECT.
        auth = _proxy_auth_header(proxy_info)
        if auth:
            headers["Proxy-authorization"] = auth
    headers["Connection"] = "keep-alive"
    if ordered_headers is not None:
        return headers
    return {name.title(): value for name, value in headers.items()}


def _exchange(conn, req, selector, scheme, proxy_info, key, reused,
              ordered_headers=None):
    """Run one request/response exchange; returns a release-aware response.
    Raises _StaleConnection (retry once), _TransportFailure, or other errors."""
    method = req.get_method()
    headers = _request_headers(req, scheme, proxy_info, ordered_headers)
    try:
        conn.request(method, selector, req.data, headers,
                     encode_chunked=req.has_header("Transfer-encoding"))
    except OSError as err:
        # As urllib: wrap send-stage OSError (including lazy connect) in URLError.
        if reused and _stale_connection_error(err):
            raise _StaleConnection(err) from None
        raise _TransportFailure(urllib.error.URLError(err)) from None
    try:
        resp = conn.getresponse()
    except ConnectionError as err:
        # RemoteDisconnected etc.; http.client already closed the connection.
        if reused and _stale_connection_error(err):
            raise _StaleConnection(err) from None
        raise _TransportFailure(err) from None
    # From here the response object owns the connection: release or discard.
    resp._pool = _POOL
    resp._pool_key = key
    resp._pool_conn = conn
    resp.url = req.get_full_url()
    resp.msg = resp.reason
    return resp


def _read_error_body(resp):
    """Drain an error body (bounded) so the connection is reusable; partial on failure."""
    try:
        return resp.read(_ERROR_BODY_MAX + 1)[:_ERROR_BODY_MAX]
    except Exception:
        return b""


def _fold_bypassed(host, proxy_raw):
    """Collapse a bypassed proxy to direct; return (proxy_info, proxy_raw).
    Non-HTTP proxies raise PoolBypass so the caller reuses urllib."""
    proxy_info = _split_proxy(proxy_raw)
    if proxy_raw and proxy_info is None:
        raise PoolBypass("proxy %r is not an http proxy" % (proxy_raw,))
    if proxy_info and _proxy_bypassed(host):
        return None, ""
    return proxy_info, proxy_raw

def egress_key(url, proxy=""):
    """Stable id of an outbound path: one physical exit shares a key.
    Failure accounting groups by it, so accounts on one exit share a counter."""
    scheme, host, port, _selector = _target_parts(url)
    proxy_raw = str(proxy or "").strip()
    try:
        _proxy_info, proxy_raw = _fold_bypassed(host, proxy_raw)
    except PoolBypass:
        pass
    return (scheme, host, port, proxy_raw)

def urlopen(req, timeout=30, proxy=""):
    """Pooled urlopen matching gateway.accounts.urlopen; retries once when a
    pooled connection was peer-closed before anything was sent."""
    # req.wb_exact_headers: ordered pairs sent with verbatim names (chat path).
    scheme, host, port, selector = _target_parts(req.full_url)
    proxy_raw = str(proxy or "").strip()
    # A bypassed host is direct (its key is the direct pool), matching urllib.
    proxy_info, proxy_raw = _fold_bypassed(host, proxy_raw)
    if proxy_info and scheme == "http":
        # HTTP via proxy: the request target is the absolute URL (as urllib).
        selector = req.full_url
    ordered_headers = _exact_header_pairs(getattr(req, "wb_exact_headers", None))
    # Key by (target, proxy, credential): different exits never share a
    # connection; different accounts on one exit never share either.
    key = (scheme, host, port, proxy_raw,
           _identity_discriminator(req, ordered_headers))

    def factory():
        return _new_connection(scheme, host, port, proxy_info, timeout)

    conn, reused = _POOL.acquire(key, timeout, factory)
    # Retry only rewindable bodies (bytes/None): a file-like body would resume
    # mid-stream and send a truncated second request. All callers pass bytes.
    replayable = req.data is None or isinstance(req.data, (bytes, bytearray, memoryview))
    retried = False
    while True:
        try:
            resp = _exchange(conn, req, selector, scheme, proxy_info, key, reused,
                             ordered_headers)
        except _StaleConnection as stale:
            _POOL.discard(conn)
            if reused and replayable and not retried:
                # Race the probe missed: the peer closed the connection after
                # the check. Retry once on a fresh connection, never a pooled one.
                retried = True
                reused = False
                conn = factory()
                _POOL.note_created()
                continue
            raise stale.original
        except _TransportFailure as failure:
            _POOL.discard(conn)
            raise failure.original
        except PoolBypass:
            # The response object already released or closed the connection.
            raise
        except Exception:
            # Unknown connection state (protocol error, timeout): close, do not release.
            _POOL.discard(conn)
            raise
        status = resp.status
        if 200 <= status < 300:
            return resp
        # Non-2xx: drain the body (bounded); its completeness decides whether
        # the connection is released or discarded.
        body = _read_error_body(resp)
        resp.close()
        if 300 <= status < 400:
            # Hand redirects (relative Location, host change, POST->GET) back
            # to urllib unchanged. 3xx never occurs on these JSON APIs.
            raise PoolBypass("upstream answered %d; replayed via urllib" % status)
        raise urllib.error.HTTPError(req.full_url, status, resp.reason,
                                     resp.headers, io.BytesIO(body))
