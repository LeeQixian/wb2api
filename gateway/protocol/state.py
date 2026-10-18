"""Shared mutable protocol state; read as attributes, never import by value."""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: accounts dir holding settings.json switches; server.main() overrides it.
ACCOUNTS_DIR = os.environ.get("WB_ACCOUNTS_DIR") or os.path.join(_ROOT, "accounts")

#: active upstream exit; kept in sync by upstream.realm.set_realm().
CURRENT_REALM = os.environ.get("WB_PROXY_DEFAULT_REALM", "intl")

#: system prompt injected when the client sends none (passthrough mode).
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT

#: --system-prompt-file override for custom/append prompt modes.
PROMPT_FILE_OVERRIDE = ""

#: cached /v1/models payloads per realm.
MODELS_CACHE = {"intl": {"at": 0.0, "data": None}, "cn": {"at": 0.0, "data": None}}


def set_realm(realm):
    """Pin the global exit used by requests that name no realm."""
    global CURRENT_REALM
    CURRENT_REALM = realm


def set_system_prompt(text):
    """Set the injected system prompt (passthrough placeholder or custom text)."""
    global SYSTEM_PROMPT
    SYSTEM_PROMPT = text
