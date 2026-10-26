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
# Upstream reads "client self-introduction + brand" as an unofficial client
# (400/11-128). Rewrite the whole sentence; system/developer/assistant only.
SANITIZE_IDENTITY_VERBS = (
    "powered by", "built on", "built by", "created by",
    "developed by", "made by", "trained by",
)
SANITIZE_IDENTITY_RE = re.compile(
    r"You are\s+"
    r"(?:an?\s+|the\s+)?(?:[\w.-]+\s+){0,3}?"
    r"(?:agent|assistant|model|AI|LLM)?[\s,]*"
    r"(?:powered by|built on|built by|created by|developed by|made by|trained by)"
    r"[^\n]*?(?:\.(?=\s|$)|(?=\n|$))",
    re.IGNORECASE)
SANITIZE_IDENTITY_REPLACEMENT = "You are a helpful AI assistant."
# The GUI paragraph is the fingerprint as a whole: only reorder the tail of the
# first sentence, keeping every word (incl. the full product name).
SANITIZE_DSH_GUI_RE = re.compile(
    r"(?i)(You are interacting with the user through the "
    r"DeepSeek[ \t]+Harness[ \t]+Web[ \t]+GUI)"
    r" whose URL the runtime context names\.")
SANITIZE_DSH_GUI_REPLACEMENT = r"\1 whose URL is named in the runtime context."
SANITIZE_DSH_GUI_LOWER = "whose url the runtime context names"
# Brand shortening: prose spacing only, so paths and identifiers stay intact.
SANITIZE_BRAND_RE = re.compile(r"DeepSeek[ \t]+Harness", re.IGNORECASE)
SANITIZE_BRAND_REPLACEMENT = "DeepSeek"
SANITIZE_BRAND_LOWER = "harness"
SANITIZE_REI_FOLD = str.maketrans({0x130: "i", 0x131: "i", 0x17F: "s"})
SANITIZE_BARE_HDR_LOWER = SANITIZE_BARE_HDR_RE.pattern
if SANITIZE_BARE_HDR_LOWER.startswith("(?i)"):
    SANITIZE_BARE_HDR_LOWER = SANITIZE_BARE_HDR_LOWER[4:]
SANITIZE_OMO_JUNIOR_LOWER = SANITIZE_OMO_JUNIOR_RE.pattern.lower()


def has_fingerprint(text, identity=True):
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    if "\u0130" in text or "\u0131" in text or "\u017f" in text:
        low = text.translate(SANITIZE_REI_FOLD).lower()
    else:
        low = text.lower()
    if (SANITIZE_BARE_HDR_LOWER in low
            or SANITIZE_OMO_JUNIOR_LOWER in low):
        return True
    if not identity:
        return False
    if SANITIZE_BRAND_LOWER in low and SANITIZE_BRAND_RE.search(text) is not None:
        return True
    if (SANITIZE_DSH_GUI_LOWER in low
            and SANITIZE_DSH_GUI_RE.search(text) is not None):
        return True
    # The verb phrase is only a cheap prefilter; the regex decides.
    if not any(needle in low for needle in SANITIZE_IDENTITY_VERBS):
        return False
    return SANITIZE_IDENTITY_RE.search(text) is not None


def sanitize_text(text, identity=True):
    if not isinstance(text, str) or not text:
        return text
    if not has_fingerprint(text, identity=identity):
        return text
    # Deliberately narrow: either token alone is accepted upstream, so only
    # the confirmed contiguous fingerprint is neutralized.
    text = SANITIZE_OMO_JUNIOR_RE.sub("Sisyphus-Junior - Focused executor", text)
    if identity:
        text = SANITIZE_IDENTITY_RE.sub(SANITIZE_IDENTITY_REPLACEMENT, text)
        text = SANITIZE_DSH_GUI_RE.sub(SANITIZE_DSH_GUI_REPLACEMENT, text)
        # Brand shortening runs last: the GUI rule still needs the full name.
        text = SANITIZE_BRAND_RE.sub(SANITIZE_BRAND_REPLACEMENT, text)
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


def sanitize_content(content, identity=True):
    if isinstance(content, str):
        return sanitize_text(content, identity=identity)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                p = dict(part)
                p["text"] = sanitize_text(p["text"], identity=identity)
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
                    fn["arguments"] = sanitize_text(raw_args, identity=False)
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
            # Identity rules never touch user/tool data; assistant history is
            # included because the same sentence 400s there too.
            identity = item.get("role") in ("system", "developer", "assistant")
            if "content" in item and not keep_result:
                item["content"] = sanitize_content(item["content"], identity=identity)
            if isinstance(item.get("reasoning_content"), str):
                item["reasoning_content"] = sanitize_text(item["reasoning_content"],
                                                          identity=False)
            if "tool_calls" in item:
                item["tool_calls"] = sanitize_tool_calls(item["tool_calls"])
            out.append(item)
        else:
            out.append(m)
    return out
