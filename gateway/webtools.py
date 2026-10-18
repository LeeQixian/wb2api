# -*- coding: utf-8 -*-
"""Gateway-run web_search / web_fetch (switch: settings.local_web_tools).
No upstream search backend, so the gateway runs the calls itself (DuckDuckGo)."""

import html as _html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

WEB_SEARCH_NAME = "web_search"
WEB_FETCH_NAME = "web_fetch"

    # Clients declare the same tool under these types.
SEARCH_DECL_TYPES = ("web_search", "web_search_preview", "web_search_preview_2025_03_11")
FETCH_DECL_TYPES = ("web_fetch",)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_RESULTS = 10
MAX_FETCH_CHARS = 100000
HTTP_TIMEOUT = 20
SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"


def max_rounds():
    """Max web-tool rounds the gateway runs; env-overridable."""
    try:
        n = int(os.environ.get("WB_MAX_WEB_ROUNDS", "") or "")
    except (TypeError, ValueError):
        n = 0
    return min(8, n) if n > 0 else 3


MAX_WEB_ROUNDS = max_rounds()


def web_search_tool_def():
    return {
        "type": "function",
        "name": WEB_SEARCH_NAME,
        "description": (
            "Searches the web for real-time information and returns ranked results "
            "with titles, URLs and snippets. Use it for current events, documentation "
            "lookup, or anything beyond your knowledge cutoff. To read a page in full, "
            "call web_fetch on its URL afterwards."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query (at least 2 characters).",
                },
                "numResults": {
                    "type": "number",
                    "description": "How many results to return (1-10, default 5).",
                },
            },
            "required": ["query"],
        },
    }


def web_fetch_tool_def():
    return {
        "type": "function",
        "name": WEB_FETCH_NAME,
        "description": (
            "Fetches a URL and returns its readable text. Use startIndex to page "
            "through a long page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http:// or https:// URL to fetch.",
                },
                "startIndex": {
                    "type": "number",
                    "description": "Character offset to continue reading a long page.",
                },
            },
            "required": ["url"],
        },
    }


def _declared(tools, types):
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("type") or "").strip().lower() in types:
            return True
        # Some clients wrap it in a function shape.
        if str(t.get("name") or "").strip().lower() in types:
            return True
    return False


def client_wants_web(tools):
    """Which web tools the client declared (server-side or function shape)."""
    return {
        "search": _declared(tools, SEARCH_DECL_TYPES + (WEB_SEARCH_NAME,)),
        "fetch": _declared(tools, FETCH_DECL_TYPES + (WEB_FETCH_NAME,)),
    }


def install_tool_defs(chat_tools, wants):
    """Replace the client's web-tool declarations with our functions.
    Same-named entries go first, so upstream never sees two web_search tools."""
    names = set()
    if wants.get("search"):
        names.add(WEB_SEARCH_NAME)
    if wants.get("fetch"):
        names.add(WEB_FETCH_NAME)

    kept = []
    for t in chat_tools or []:
        if not isinstance(t, dict):
            kept.append(t)
            continue
        type_name = str(t.get("type") or "").strip().lower()
        name = str(t.get("name") or "").strip().lower()
        if isinstance(t.get("function"), dict):
            name = name or str((t.get("function") or {}).get("name") or "").strip().lower()
        if name in names or type_name in names:
            continue
        kept.append(t)

    if wants.get("search"):
        kept.append(web_search_tool_def())
    if wants.get("fetch"):
        kept.append(web_fetch_tool_def())
    return kept


def is_internal_tool(name):
    return str(name or "").strip() in (WEB_SEARCH_NAME, WEB_FETCH_NAME)


def query_args(args):
    """Pull the query string out of the tool arguments.
    Accepts query / queries / q; an array is joined with " or "."""
    if not isinstance(args, dict):
        return ""
    raw = args.get("query")
    if raw is None:
        raw = args.get("queries")
    if raw is None:
        raw = args.get("q")
    if isinstance(raw, (list, tuple)):
        parts = [str(x).strip() for x in raw if str(x or "").strip()]
        return " or ".join(parts)
    return str(raw or "").strip()


def url_arg(args):
    if not isinstance(args, dict):
        return ""
    raw = args.get("url")
    if raw is None:
        raw = args.get("urls")
    if isinstance(raw, (list, tuple)):
        for x in raw:
            if str(x or "").strip():
                return str(x).strip()
        return ""
    return str(raw or "").strip()


class _UnsafeRedirectError(Exception):
    pass


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-apply _guard_url to every redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, str(newurl or ""))
        safe_url, problem = _guard_url(target)
        if problem:
            raise _UnsafeRedirectError(problem)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def _http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except Exception:
        return raw.decode("utf-8", "replace")


def _strip_tags(text):
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"(?is)<br\s*/?>", chr(10), text)
    text = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", chr(10), text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = text.replace(chr(160), " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", chr(10), text)
    text = re.sub(chr(10) + "{3,}", chr(10) + chr(10), text)
    return text.strip()


def _ddg_target(href):
    """Unwrap DuckDuckGo's /l/?uddg= redirect."""
    href = _html.unescape(str(href or "").strip())
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            target = (urllib.parse.parse_qs(parsed.query).get("uddg") or [""])[0]
            if target:
                return urllib.parse.unquote(target)
    except Exception:
        pass
    return href


def search(query, num_results=5):
    """DuckDuckGo HTML search; returns a readable string for the model."""
    query = str(query or "").strip()
    if len(query) < 2:
        return ('Error: web_search needs a query of at least 2 characters; '
                'got %r. Pass it as {"query": "..."}.' % query)
    try:
        n = int(num_results)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(MAX_RESULTS, n))

    url = SEARCH_ENDPOINT + "?" + urllib.parse.urlencode({"q": query})
    try:
        page = _http_get(url)
    except _UnsafeRedirectError as exc:
        return "Error: search redirect blocked (%s)." % exc
    except urllib.error.HTTPError as exc:
        return "Error: the search backend answered HTTP %s for %r." % (exc.code, query)
    except Exception as exc:
        return "Error: could not reach the search backend for %r (%s)." % (
            query, type(exc).__name__)

    blocks = re.split(r'(?is)<div[^>]+class="[^"]*result__body[^"]*"', page)
    results = []
    for block in blocks[1:]:
        m_link = re.search(
            r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m_link:
            continue
        href = _ddg_target(m_link.group(1))
        title = _strip_tags(m_link.group(2))
        m_snip = re.search(r'(?is)class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block)
        snippet = _strip_tags(m_snip.group(1)) if m_snip else ""
        if not href or not title:
            continue
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            break

    if not results:
        return ("No results found for: %s%sTry a broader or differently worded query."
                % (query, chr(10) + chr(10)))

    lines = ["%d. %s%s   %s%s   %s" % (i, r["title"], chr(10), r["url"], chr(10), r["snippet"])
             for i, r in enumerate(results, 1)]
    return ("Search results for: %s%s%s%s%sCite the sources you used at the end of "
            "your answer." % (query, chr(10) + chr(10), (chr(10) + chr(10)).join(lines),
                              chr(10) + chr(10), ""))


def _guard_url(url):
    """Allow only ordinary public http(s) URLs."""
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except Exception:
        return None, "Invalid URL."
    if parsed.scheme.lower() not in ("http", "https"):
        return None, "Only http:// or https:// URLs are supported."
    if parsed.username or parsed.password:
        return None, "Credentials in the URL are not allowed."
    host = (parsed.hostname or "").lower()
    if not host or "." not in host or host.endswith(".localhost"):
        return None, "Private, loopback or single-label hosts are not allowed."
    if re.match(r"^(127\.|10\.|192\.168\.|169\.254\.|0\.)", host) or \
            re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host):
        return None, "Private network addresses are not allowed."
    return url, ""


def fetch(url, start_index=0):
    url, problem = _guard_url(url)
    if problem:
        return "Error: %s" % problem
    try:
        page = _http_get(url)
    except _UnsafeRedirectError as exc:
        return "Error: redirect blocked (%s)." % exc
    except urllib.error.HTTPError as exc:
        return "Error: %s answered HTTP %s." % (url, exc.code)
    except Exception as exc:
        return "Error: could not fetch %s (%s)." % (url, type(exc).__name__)

    text = _strip_tags(page)
    try:
        start = max(0, int(start_index))
    except (TypeError, ValueError):
        start = 0
    if start >= len(text):
        return ("Error: startIndex %d is past the end of the page (%d characters "
                "total)." % (start, len(text)))

    end = min(start + MAX_FETCH_CHARS, len(text))
    head = "URL: %s%sCharacters: %d-%d of %d" % (url, chr(10), start, end, len(text))
    tail = ""
    if end < len(text):
        tail = (chr(10) + chr(10) + "[Truncated. Call web_fetch again with startIndex=%d "
                "to continue.]" % end)
    return head + chr(10) + chr(10) + text[start:end] + tail


def sources_from_result(result):
    """Recover (title, url) pairs from the search result text.
    Parses our own rendered format, since the client needs structured sources."""
    out = []
    pattern = r"(?m)^\d+\.\s*(.+?)\s*\n\s*(https?://\S+)\s*$"
    for m in re.finditer(pattern, str(result or "")):
        url = m.group(2).strip().rstrip(".,;:!?")
        title = m.group(1).strip()
        if url and not any(s["url"] == url for s in out):
            out.append({"title": title or url, "url": url})
    return out


def execute(name, args_raw):
    """Run one internal web tool; never raises, always returns model-feedable text."""
    name = str(name or "").strip()
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw or "{}")
        except Exception:
            args = {}
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = {}
    if not isinstance(args, dict):
        args = {}

    try:
        if name == WEB_SEARCH_NAME:
            return search(query_args(args), args.get("numResults") or 5)
        if name == WEB_FETCH_NAME:
            return fetch(url_arg(args), args.get("startIndex") or 0)
        return "Error: %s is not a tool this gateway runs." % name
    except Exception as exc:
        return "Error running %s: %s: %s" % (name, type(exc).__name__, exc)
