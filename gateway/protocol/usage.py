"""Usage normalization across client spellings."""


def _best_cached_tokens(usage):
    """Best cache-hit value across every alias the upstreams emit (E3).

    Order mirrors the panel: prompt_tokens_details.cached_tokens >
    prompt_cache_hit_tokens > cache_read_input_tokens > cached_tokens >
    input_tokens_details.cached_tokens > completion_tokens_details.
    First positive value wins; zero aliases never shadow a real hit.
    """
    if not isinstance(usage, dict):
        return 0
    prompt_details = usage.get("prompt_tokens_details") or {}
    input_details = usage.get("input_tokens_details") or {}
    details = usage.get("completion_tokens_details") or {}
    for value in (prompt_details.get("cached_tokens"),
                  usage.get("prompt_cache_hit_tokens"),
                  usage.get("cache_read_input_tokens"),
                  usage.get("cached_tokens"),
                  input_details.get("cached_tokens"),
                  details.get("cached_tokens")):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            # Token counts must stay integers: the Codex client parses
            # response.completed strictly and rejects 123.0 (invalid number).
            return int(number)
    return 0


def normalize_usage_cache_aliases(usage):
    """Write the best cache-hit value into every alias.

    Some responses carry the real hit in prompt_tokens_details.cached_tokens
    while also emitting cache_read_input_tokens: 0 / cached_tokens: 0
    compatibility aliases; strict downstream parsers may prefer the zero
    aliases and lose the hit. Mutates and returns the usage dict.
    """
    if not isinstance(usage, dict):
        return usage
    best = _best_cached_tokens(usage)
    if best <= 0:
        return usage
    usage["cache_read_input_tokens"] = best
    usage["cached_tokens"] = best
    usage["prompt_cache_hit_tokens"] = best
    prompt_details = dict(usage.get("prompt_tokens_details") or {})
    prompt_details["cached_tokens"] = best
    usage["prompt_tokens_details"] = prompt_details
    if isinstance(usage.get("input_tokens_details"), dict):
        input_details = dict(usage["input_tokens_details"])
        input_details["cached_tokens"] = best
        usage["input_tokens_details"] = input_details
    return usage


def _responses_usage(u):
    if not u:
        return None
    det = u.get("completion_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens") or 0,
        "input_tokens_details": {
            "cached_tokens": _best_cached_tokens(u),
        },
        "output_tokens": u.get("completion_tokens") or 0,
        "output_tokens_details": {"reasoning_tokens": det.get("reasoning_tokens") or 0},
        "total_tokens": u.get("total_tokens") or 0,
    }
