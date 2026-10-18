"""Outbound identities (desktop / VSCode / CLI); single source of versions/UAs.
All three must send the conversation header family or upstream 400s (code 11-128)."""

import gzip
import hashlib
import io
import os
import platform
import secrets
import time
import uuid

# Desktop intl per the 5.8.4 capture; UA tail is the bundled CLI version.
DESKTOP_VERSION_INTL = "5.8.4"
DESKTOP_UA_INTL = "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0"
# Desktop cn: no complete capture after 5.5.6; pinned, never guessed forward.
DESKTOP_VERSION_CN = "5.5.6"
DESKTOP_UA_CN = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"

# Official CodeBuddy CLI version (npm @tencent-ai/codebuddy-code unpacked).
DEFAULT_IDE_VERSION = "2.168.2"

# OpenAI Node SDK 6.25.0 fingerprint headers on the CLI chat wire; host
# defaults overridable via WB_IDENTITY_OS / _ARCH / _NODE_VERSION.
def _identity_os():
    override = str(os.environ.get("WB_IDENTITY_OS") or "").strip()
    if override:
        return override
    system = platform.system()
    return {"Windows": "Windows", "Darwin": "MacOS", "Linux": "Linux"}.get(
        system, system or "Linux")


def _identity_arch():
    override = str(os.environ.get("WB_IDENTITY_ARCH") or "").strip()
    if override:
        return override
    machine = (platform.machine() or "").lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64",
            "arm64": "arm64", "i386": "ia32", "i686": "ia32"}.get(
        machine, machine or "x64")


def cli_stainless_headers():
    """x-stainless-* headers the official CLI's SDK sends."""
    return {
        "x-stainless-lang": "js",
        "x-stainless-package-version": "6.25.0",
        "x-stainless-os": _identity_os(),
        "x-stainless-arch": _identity_arch(),
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": (
            str(os.environ.get("WB_IDENTITY_NODE_VERSION") or "").strip()
            or "v22.21.1"),
        "x-stainless-retry-count": "0",
        "x-stainless-timeout": "1200",
    }


# Official AGENT_RUNTIMES fallback for unknown/absent runtime.
CLI_AGENT_RUNTIME = "other"
# RequestBodyGzip=true: gzip'd chat bodies carry this header.
REQUEST_CONTENT_ENCODING = "gzip"
# resolveCacheRetention default injected into CLI chat bodies.
CLI_CACHE_RETENTION = "long"
# Fields the official CLI never sends in a chat body.
CLI_BODY_DROP_FIELDS = ("metadata", "conversation_id", "previous_response_id")

# Official VSCode extension 4.12.39997873; no host IDE here, so
# WB_VSCODE_IDE_VERSION overrides the platform version segment.
VSCODE_PRODUCT_VERSION = "4.12.39997873"
VSCODE_IDE_VERSION = "1.119.0"
VSCODE_PLATFORM = "VSCode"
VSCODE_PRODUCT_NAME = "CodeBuddy"
VSCODE_ENV_ID = "production"


def vscode_ide_version():
    return (str(os.environ.get("WB_VSCODE_IDE_VERSION") or "").strip()
            or VSCODE_IDE_VERSION)


def vscode_ua():
    return "%s/%s %s/%s" % (VSCODE_PLATFORM, vscode_ide_version(),
                            VSCODE_PRODUCT_NAME, VSCODE_PRODUCT_VERSION)


# Legacy aliases kept for external scripts reading the old names.
DEFAULT_WORKBUDDY_IDE_VERSION = VSCODE_IDE_VERSION
DEFAULT_WORKBUDDY_PRODUCT_VERSION = VSCODE_PRODUCT_VERSION

PRODUCT_DESKTOP = "workbuddy"
PRODUCT_VSCODE = "vscode"
PRODUCT_CLI = "cli"
VALID_PRODUCTS = (PRODUCT_DESKTOP, PRODUCT_VSCODE, PRODUCT_CLI)


def normalize_product(value):
    """Normalize any spelling to 'workbuddy', 'vscode' or 'cli'."""
    v = str(value or "").strip().lower()
    if v in ("workbuddy", "wb", "desktop"):
        return PRODUCT_DESKTOP
    if v in ("vscode", "vsc", "ide"):
        return PRODUCT_VSCODE
    if v in ("cli", "codebuddy"):
        return PRODUCT_CLI
    return PRODUCT_DESKTOP


_ENDPOINTS = {
    ("intl", PRODUCT_DESKTOP): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    ("intl", PRODUCT_VSCODE): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    # www.codebuddy.ai does not resolve here (sinkholed to 0.0.0.1), so the
    # intl CLI identity rides the workbuddy.ai edge, which accepts CLI headers.
    ("intl", PRODUCT_CLI): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    ("cn", PRODUCT_DESKTOP): ("https://copilot.tencent.com", "copilot.tencent.com"),
    ("cn", PRODUCT_VSCODE): ("https://www.workbuddy.cn", "www.workbuddy.cn"),
    ("cn", PRODUCT_CLI): ("https://copilot.tencent.com", "copilot.tencent.com"),
}


def endpoint_for(realm, product):
    """Return (chat base URL, X-Domain); it changes with the identity."""
    key = ("cn" if realm == "cn" else "intl", normalize_product(product))
    return _ENDPOINTS[key]


def domain_for_realm(realm):
    """X-Domain value for this realm; defaults to the desktop one."""
    return endpoint_for(realm, PRODUCT_DESKTOP)[1]


def uuid7():
    """RFC 9562 UUIDv7 (ms timestamp + random); upstream accepts v4/v7 alike."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    return uuid.UUID(int=(ms << 80) | (0x7 << 76) | (rand_a << 64)
                     | (0b10 << 62) | rand_b)


def new_conversation_id():
    """X-Conversation-ID: hyphenated UUID (5.8.4 shape)."""
    return str(uuid7())


def conversation_id_for(uid, session_key):
    """Conversation id stable per (account, session key); '' without a key."""
    key = str(session_key or "").strip()
    if not key:
        return ""
    digest = hashlib.sha256(("%s|%s" % (uid or "", key)).encode("utf-8")).hexdigest()
    h = list(digest[:32])
    h[12] = "4"          # UUID version nibble
    h[16] = "a"          # RFC 4122 variant nibble
    h = "".join(h)
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def new_message_id():
    """Request/message ids: 32-hex UUIDv7 (5.8.4 shape)."""
    return uuid7().hex


def new_turn_request_id():
    """One user send's request id; caller makes it once, retries reuse it."""
    return new_message_id()


def gzip_body(data):
    """gzip a serialized chat body; mtime=0 keeps output byte-stable."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(data)
    return buf.getvalue()


def trace_headers(with_parent=False):
    """Trace family (traceparent / b3 / X-B3-*); with_parent adds the 4th b3
    segment matching the official CLI sub-span structure."""
    trace = new_message_id()
    span = secrets.token_hex(8)
    headers = {
        "X-Trace-ID": trace,
        "traceparent": "00-%s-%s-01" % (trace, span),
        "b3": "%s-%s-1" % (trace, span),
        "X-B3-TraceId": trace,
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
    }
    if with_parent:
        parent = secrets.token_hex(8)
        headers["X-B3-ParentSpanId"] = parent
        headers["b3"] = "%s-%s-1-%s" % (trace, span, parent)
    return headers


def new_request_trace_id():
    """X-Request-Trace-Id: hyphenated UUID, fresh per request (official form)."""
    return str(uuid.uuid4())


def vscode_trace_headers():
    """VSCode trace family: four-segment b3, no traceparent; parent span is
    synthesized since there is no real call tree."""
    trace = new_message_id()
    span = secrets.token_hex(8)
    parent = secrets.token_hex(8)
    return {
        "X-Trace-ID": trace,
        "b3": "%s-%s-1-%s" % (trace, span, parent),
        "X-B3-TraceId": trace,
        "X-B3-ParentSpanId": parent,
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
    }


def build_identity_headers(product, realm, uid, token,
                           conversation_id=None, enterprise_id="",
                           tenant_id="", department="",
                           model=None, session_key=None, turn_request_id=None):
    """Identity headers for this attempt; product: workbuddy|vscode|cli.
    CLI consumes session_key/turn_request_id, VSCode consumes model."""
    product = normalize_product(product)
    is_cn = (realm == "cn")

    conv = str(conversation_id or "").strip() or (
        new_message_id() if product == PRODUCT_VSCODE else new_conversation_id())
    msg_id = new_message_id()
    conv_req_id = new_message_id()
    if product == PRODUCT_CLI:
        if not str(conversation_id or "").strip():
            derived = conversation_id_for(uid, session_key)
            if derived:
                conv = derived
        conv_req_id = str(turn_request_id or "").strip() or new_message_id()

    if product == PRODUCT_DESKTOP:
        client_ver = DESKTOP_VERSION_CN if is_cn else DESKTOP_VERSION_INTL
        chat_ua = DESKTOP_UA_CN if is_cn else DESKTOP_UA_INTL
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Type": "main",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": client_ver,
            "X-Private-Data": "true",
            "X-Product": "SaaS",
            "X-Domain": endpoint_for(realm, product)[1],
            "User-Agent": chat_ua,
            "X-User-Id": str(uid or "anonymous"),
            "Authorization": "Bearer " + str(token or ""),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Root-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": msg_id,
        }
        headers.update(trace_headers())
    elif product == PRODUCT_VSCODE:
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Type": "main",
            "X-IDE-Type": VSCODE_PLATFORM,
            "X-IDE-Name": VSCODE_PLATFORM,
            "X-IDE-Version": vscode_ide_version(),
            "X-Product-Version": VSCODE_PRODUCT_VERSION,
            "X-Env-ID": VSCODE_ENV_ID,
            "X-Product": "SaaS",
            "X-Domain": endpoint_for(realm, product)[1],
            "User-Agent": vscode_ua(),
            "X-User-Id": str(uid or "anonymous"),
            "Authorization": "Bearer " + str(token or ""),
            "X-Request-Trace-Id": new_request_trace_id(),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": conv_req_id,
        }
        if model:
            now_ms = str(int(time.time() * 1000))
            headers["X-Model-ID"] = str(model)
            headers["monitor_promptPrepareStartTime"] = now_ms
            headers["monitor_httpSendTime"] = now_ms
        # Official sends X-Private-Data on every site except cn.
        if not is_cn:
            headers["X-Private-Data"] = "true"
        headers.update(vscode_trace_headers())
    else:  # PRODUCT_CLI
        ide_version = DEFAULT_IDE_VERSION
        headers = {
            "X-Agent-Intent": "craft",
            "X-Agent-Purpose": "conversation",
            "X-Agent-Runtime": CLI_AGENT_RUNTIME,
            "X-Agent-Type": "main",
            "X-IDE-Type": "CLI",
            "X-IDE-Name": "CLI",
            "X-IDE-Version": ide_version,
            "X-Product": "SaaS",
            "X-Domain": endpoint_for(realm, product)[1],
            "User-Agent": "CLI/%s CodeBuddy/%s" % (ide_version, ide_version),
            "X-User-Id": str(uid or "anonymous"),
            "Authorization": "Bearer " + str(token or ""),
            "X-Conversation-ID": conv,
            "X-Conversation-Request-ID": conv_req_id,
            "X-Root-Request-ID": conv_req_id,
            "X-Conversation-Message-ID": msg_id,
            "X-Request-ID": msg_id,
        }
        # Official default: personal false, enterprise true.
        headers["X-Private-Data"] = "true" if enterprise_id else "false"
        headers.update(cli_stainless_headers())
        headers.update(trace_headers(with_parent=True))

    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
        headers["X-Tenant-Id"] = str(tenant_id or enterprise_id)
    if department:
        headers["X-Department-Info"] = str(department)

    return headers
