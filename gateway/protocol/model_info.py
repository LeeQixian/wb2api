"""Model id classification, realm routing and catalog metadata."""


from gateway import catalog as wb_catalog
from gateway.common.log import log
from gateway.protocol import state  # noqa: F401


INTL_EXCLUSIVE_PREFIXES = ("gpt-", "gemini-")
CN_EXCLUSIVE_PREFIXES = ("minimax-", "deepseek-v4-pro")
INTL_EXCLUSIVE = {
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
    "grok-4.7",
}
CN_EXCLUSIVE = {
    "deepseek-v4-pro", "glm-5.1", "glm-5v-turbo",
    "kimi-k3-1", "kimi-k2.7", "minimax-m3",
    "hy3-x", "hy4-preview-dev", "hy4-preview-x",
}
REALM_PREFIXES = {"cn": "cn", "global": "intl", "intl": "intl"}
VIRTUAL_ALIAS_MODELS = {
    "default-model",
    "fast-model",
    "balanced-model",
    "primary-model",
    "deep-model",
    # The domestic exit's auto-router entry: the picker shows it, but it is
    # not a model a client can pin, so it stays out of the advertised list.
    "auto",
}
NON_CHAT_MODELS = {"lite"} | VIRTUAL_ALIAS_MODELS
NON_CHAT_PREFIXES = ("codewise-", "completion-")
NON_CHAT_SUFFIXES = ("-image-alpha", "-image-alpha-edit", "-taco-completion")
ALLOWED_MODELS = {
    # Empty blocks nothing; listing ids allows only those.
}
ALLOWED_PREFIXES = ()
BANNED_SUBSTRING = ""
EXTRA_BANNED = set()


def detect_model_realm(model_id):
    if not model_id:
        return state.CURRENT_REALM
    m = str(model_id).lower()
    intl_only = {
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
        "grok-4.7"
    }
    if m in intl_only or any(m.startswith(p) for p in ("gpt-", "gemini-")):
        return "intl"
    cn_only = {
        "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
        "glm-5.1", "glm-5.0-turbo", "glm-4.6v",
        "kimi-k3-1", "kimi-k2.7", "kimi-k2-thinking",
        "hy3-x", "hy4-preview-dev", "hy4-preview-x"
    }
    if m in cn_only or any(m.startswith(p) for p in ("minimax-", "deepseek-v4-pro")):
        return "cn"
    return state.CURRENT_REALM


def exclusive_realm(model_id):
    """"intl"/"cn" when only that exit serves the model, else ""."""
    if not model_id:
        return ""
    m = str(model_id).lower()
    if m in INTL_EXCLUSIVE or m.startswith(INTL_EXCLUSIVE_PREFIXES):
        return "intl"
    if m in CN_EXCLUSIVE or m.startswith(CN_EXCLUSIVE_PREFIXES):
        return "cn"
    return ""


def split_realm_prefix(model_id):
    """Return (bare_model_id, realm_or_None) for a name like "cn:hy3"."""
    m = str(model_id or "")
    head, sep, tail = m.partition(":")
    if sep and tail:
        realm = REALM_PREFIXES.get(head.strip().lower())
        bare = tail.strip()
        if realm and bare:
            return bare, realm
    return m, None


def is_chat_model(mid):
    if not mid:
        return False
    if mid in NON_CHAT_MODELS:
        return False
    if mid.startswith(NON_CHAT_PREFIXES):
        return False
    if mid.endswith(NON_CHAT_SUFFIXES):
        return False
    return True


def is_model_banned(model):
    """True when this model name must not be sent upstream.
    Allow-lists pass; others are rejected only on a banned-substring hit."""
    if not model:
        return False
    m = str(model).strip().lower()
    # Allow-list wins, including -high / -preview / [1M] variants.
    if m in ALLOWED_MODELS:
        return False
    if any(m.startswith(a) for a in ALLOWED_PREFIXES):
        return False
    # Deny when the model name contains this substring.
    if BANNED_SUBSTRING and BANNED_SUBSTRING in m:
        return True
    # Other known internal models that burn credits.
    if m in EXTRA_BANNED:
        return True
    return False


def model_catalog_meta(model):
    """The catalog metadata dict for a model, or {}.
    Side-effect free: reads the populated cache and static tables only."""
    if not model:
        return {}
    try:
        realm = detect_model_realm(model) or state.CURRENT_REALM
        entries = (state.MODELS_CACHE.get(realm) or {}).get("data")
        if not entries:
            name = "STATIC_CN_MODELS" if realm == "cn" else "STATIC_INTL_MODELS"
            table = getattr(wb_catalog, name, None) or wb_catalog.STATIC_MODELS
            entries = [(m.get("id"), m) for m in table if isinstance(m, dict)]
        m_lower = str(model).strip().lower()
        for mid, meta in entries:
            if str(mid).strip().lower() == m_lower:
                return meta or {}
    except Exception as exc:
        log("catalog meta lookup failed for '%s': %s" % (model, exc))
    return {}


def model_reasoning_meta(model):
    """The catalog's reasoning block for a model, or {}."""
    return model_catalog_meta(model).get("reasoning") or {}


def model_default_max_output_tokens(model):
    """The max output tokens the catalog declares for a model, or None."""
    val = model_catalog_meta(model).get("maxOutputTokens")
    if val is not None:
        try:
            val = int(val)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return None


def model_default_effort(model):
    """The effort the catalog applies when the client asks for none, or None."""
    effort = model_reasoning_meta(model).get("defaultEffort")
    if isinstance(effort, str) and effort.strip():
        return effort.strip()
    return None
