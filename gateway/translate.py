"""Protocol facade: re-exports every gateway.protocol.* symbol as translate.X."""
import re  # noqa: F401  (kept for callers that reach through this module)
import sys
import types

from gateway.protocol import state  # noqa: F401
from gateway.protocol.state import set_realm, set_system_prompt  # noqa: F401
from gateway.common.log import log  # noqa: F401
from gateway import prompt as wb_prompt  # noqa: F401  (test patch point)

# Live proxies: reads and writes mirror gateway.protocol.state so that
# translate.CURRENT_REALM = x keeps working for callers and tests.
_PROXY_STATE = ("ACCOUNTS_DIR", "CURRENT_REALM", "DEFAULT_SYSTEM_PROMPT",
                "SYSTEM_PROMPT", "PROMPT_FILE_OVERRIDE", "_models_cache")


class _FacadeModule(types.ModuleType):
    def __getattr__(self, name):
        if name in _PROXY_STATE:
            return getattr(state, "MODELS_CACHE" if name == "_models_cache" else name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in _PROXY_STATE:
            setattr(state, "MODELS_CACHE" if name == "_models_cache" else name, value)
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule
from gateway.protocol.state import MODELS_CACHE as _models_cache  # noqa: F401

from gateway.protocol.sanitize import (  # noqa: F401
    SANITIZE_BARE_HDR_LOWER, SANITIZE_BARE_HDR_RE, SANITIZE_FEATURES,
    SANITIZE_HDR_RE, SANITIZE_KV_RE, SANITIZE_OMO_JUNIOR_LOWER,
    SANITIZE_OMO_JUNIOR_RE, SANITIZE_REI_FOLD, SANITIZE_REWRITES,
    has_fingerprint, sanitize_content, sanitize_messages, sanitize_text,
    sanitize_tool_calls,
)
from gateway.protocol.repair import (  # noqa: F401
    _empty_content, _merge_reasoning_content, backfill_reasoning_content,
    cleanup_orphan_tool_calls, drop_truncated_tool_calls,
    is_truncated_arguments, merge_adjacent_tool_calls, normalize_roles,
    repack_tool_result_blocks,
)
from gateway.protocol.model_info import (  # noqa: F401
    ALLOWED_MODELS, ALLOWED_PREFIXES, BANNED_SUBSTRING, CN_EXCLUSIVE,
    CN_EXCLUSIVE_PREFIXES, EXTRA_BANNED, INTL_EXCLUSIVE,
    INTL_EXCLUSIVE_PREFIXES, NON_CHAT_MODELS, NON_CHAT_PREFIXES,
    NON_CHAT_SUFFIXES, REALM_PREFIXES, VIRTUAL_ALIAS_MODELS,
    detect_model_realm, exclusive_realm, is_chat_model, is_model_banned,
    model_catalog_meta, model_default_effort, model_default_max_output_tokens,
    model_reasoning_meta, split_realm_prefix,
)
from gateway.protocol.usage import (  # noqa: F401
    _best_cached_tokens, _responses_usage, normalize_usage_cache_aliases,
)
from gateway.protocol.stream import (  # noqa: F401
    NOISE_KEYS, _strip_empty_fc, aggregate_stream, clean_chunk,
    clean_responses_frame, strip_data_prefix,
)
from gateway.protocol.tools import (  # noqa: F401
    CUSTOM_TOOL_HINT, DSML_CALLS_RE, DSML_INVOKE_RE, DSML_PARAM_RE,
    NAMESPACE_MAX_DEPTH, NAMESPACE_TOOL_MESSAGE, REJECT_NAMESPACE_TOOLS,
    _downgrade_custom_tool, _is_custom_tool, _tools_for_chat,
    _unwrap_custom_input, additional_tools_from_input,
    apply_namespace_to_calls, custom_tool_names, expand_namespace_tools,
    find_namespace_tool, parse_dsml_tool_calls, resolve_namespaced_name,
    stamp_namespace,
)
from gateway.protocol.request import (  # noqa: F401
    AFFINITY_BY_PREFIX, AFFINITY_DEBUG, BACKGROUND_THREAD_SOURCES,
    BACKGROUND_TRIGGER_KEYWORDS, BLOCK_BACKGROUND_REQUESTS, CJK_RE,
    background_request_message, background_request_reason,
    banned_model_message, build_citations, build_upstream_body,
    derive_affinity_key, estimate_tokens, inject_prompt_cache_key,
    is_compaction_request, local_web_tools_enabled, normalize_stop,
    normalize_tool_choice, normalize_tools, prompt_cache_key_enabled,
    prompt_fingerprint, prompt_mode_config, sum_usage,
    translate_max_completion_tokens, turn_metadata_fields, web_tools_active,
)
from gateway.protocol.responses_in import (  # noqa: F401
    _flatten_content, _new_id, _responses_input_to_messages, responses_to_chat,
)
from gateway.protocol.responses_out import (  # noqa: F401
    chat_to_response, follow_up_with_tool_results, internal_calls_from_chat,
    stream_responses_events, tool_name_of,
)
