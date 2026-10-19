"""Fingerprint sanitization; immunizes Codex/Claude Code WAF patterns."""


import json
import os
import re


def preserve_request_contract_enabled():
    """Opt-in WB_PRESERVE_REQUEST_CONTRACT: keep client data verbatim where
    sanitization would rewrite it; upstream repairs still apply."""
    return os.environ.get("WB_PRESERVE_REQUEST_CONTRACT", "0").strip().lower() in (
        "1", "true", "yes", "on")


SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11128",
)
SANITIZE_REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("11128", "11-128"),
)
SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\r\n]*;?\s*")
SANITIZE_BARE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header")
SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\r\n]*;?\s*")
SANITIZE_OMO_JUNIOR_RE = re.compile(
    r"Sisyphus-Junior - Focused executor from OhMyOpenCode", re.IGNORECASE
)
SANITIZE_REI_FOLD = str.maketrans({0x130: "i", 0x131: "i", 0x17F: "s"})
SANITIZE_BARE_HDR_LOWER = SANITIZE_BARE_HDR_RE.pattern
if SANITIZE_BARE_HDR_LOWER.startswith("(?i)"):
    SANITIZE_BARE_HDR_LOWER = SANITIZE_BARE_HDR_LOWER[4:]
SANITIZE_OMO_JUNIOR_LOWER = SANITIZE_OMO_JUNIOR_RE.pattern.lower()


def has_fingerprint(text):
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    if "\u0130" in text or "\u0131" in text or "\u017f" in text:
        low = text.translate(SANITIZE_REI_FOLD).lower()
    else:
        low = text.lower()
    return SANITIZE_BARE_HDR_LOWER in low or SANITIZE_OMO_JUNIOR_LOWER in low


def sanitize_text(text):
    if not isinstance(text, str) or not text:
        return text
    if not has_fingerprint(text):
        return text
    # Deliberately narrow: either token alone is accepted upstream, so only
    # the confirmed contiguous fingerprint is neutralized.
    text = SANITIZE_OMO_JUNIOR_RE.sub("Sisyphus-Junior - Focused executor", text)
    for old, new in SANITIZE_REWRITES:
        text = text.replace(old, new)
    text = SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = ""
        while prev != text:
            prev = text
            text = SANITIZE_KV_RE.sub("", text)
    text = SANITIZE_BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
    return text.strip()


def sanitize_content(content):
    if isinstance(content, str):
        return sanitize_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                p = dict(part)
                p["text"] = sanitize_text(p["text"])
                out.append(p)
            else:
                out.append(part)
        return out
    return content


def sanitize_tool_calls(tool_calls):
    # Arguments are application data, not a client fingerprint.
    if preserve_request_contract_enabled():
        return tool_calls
    if not isinstance(tool_calls, list):
        return tool_calls
    out = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            item = dict(tc)
            fn = item.get("function")
            if isinstance(fn, dict):
                fn = dict(fn)
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    fn["arguments"] = sanitize_text(raw_args)
                elif raw_args is None:
                    fn["arguments"] = "{}"
                else:
                    # Upstream wants arguments as a JSON string; dicts/lists
                    # sent raw come back 400 code 11101. Serialize them.
                    try:
                        fn["arguments"] = json.dumps(raw_args, ensure_ascii=False)
                    except Exception:
                        fn["arguments"] = str(raw_args)
                item["function"] = fn
            out.append(item)
        else:
            out.append(tc)
    return out


def sanitize_messages(messages):
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            item = dict(m)
            keep_result = (preserve_request_contract_enabled()
                           and item.get("role") in ("tool", "function"))
            if "content" in item and not keep_result:
                item["content"] = sanitize_content(item["content"])
            if isinstance(item.get("reasoning_content"), str):
                item["reasoning_content"] = sanitize_text(item["reasoning_content"])
            if "tool_calls" in item:
                item["tool_calls"] = sanitize_tool_calls(item["tool_calls"])
            out.append(item)
        else:
            out.append(m)
    return out
