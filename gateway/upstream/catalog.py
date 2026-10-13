"""Live /v3/config catalogue fetch, merge and curation."""

from gateway import protocol as _protocol
from gateway.protocol.model_info import is_chat_model
from gateway.upstream import state as _state
from gateway.upstream.realm import get_realm
import json
import os
import time
import threading
import urllib.request
from gateway import catalog as wb_catalog
from gateway import identity as wb_identity
from gateway import settings as wb_settings
from gateway import translate as tr
from gateway.common.log import log
from gateway.common import paths
from gateway import accounts as wb_accounts


UPSTREAM = "https://www.workbuddy.ai"
CHAT_PATH = "/v2/chat/completions"
MODELS_PATH = "/v2/enterprises/personal/models"
REMOTE_CONFIG_PATH = "/v3/config"
VARIANT_SUFFIXES = ("-sg", "-x")
CN_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-pro",
]
INTL_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "grok-4.7",
    "gemini-3.5-flash",
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
    "kimi-k2.8-preview",
]
_catalog_lock = threading.Lock()


def merge_reasoning(base, live):
    """Field-level merge of one model's reasoning block.

    The live catalogue is the source of truth, but it does not always restate
    every field: the desktop endpoint sometimes gives a bare `effort` where the
    bundled table knows the level is selectable. A top-level update would drop
    `supportedEfforts`, and an `effort` without it reads as a pin - the model
    then looks unselectable in /v1/models and every request to it is reported at
    the wrong level. So the two blocks are merged field by field, and a block
    that ends up with `supportedEfforts` is selectable: its `effort` is the
    default restated, not a pin.
    """
    out = dict(base) if isinstance(base, dict) else {}
    if isinstance(live, dict):
        out.update(live)
    if out.get("supportedEfforts") and out.get("effort"):
        out.setdefault("defaultEffort", out["effort"])
        out.pop("effort", None)
    return out


def merge_catalog(primary, realm=None, extras=False):
    r = realm or get_realm()
    merged = {}
    # "all" is the union of both realms: a combined view must not drop the
    # models that only one side's catalog knows about.
    if r == "all":
        source_static = list(wb_catalog.STATIC_INTL_MODELS) + list(wb_catalog.STATIC_CN_MODELS)
    else:
        source_static = getattr(wb_catalog, "STATIC_CN_MODELS" if r == "cn"
                                else "STATIC_INTL_MODELS", wb_catalog.STATIC_MODELS)
    for item in source_static:
        mid = item.get("id")
        # First catalog wins for a shared id, so the intl entry is not
        # overwritten by its cn counterpart when both are merged.
        if mid and is_chat_model(mid) and mid not in merged:
            merged[mid] = dict(item)
    for mid, meta in primary or []:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            # #177: the live catalogue is the source of truth, but it does not
            # always restate every reasoning field: a bare `effort` would read
            # as a pin and drop the bundled `supportedEfforts`, making the
            # model look unselectable in /v1/models. Capture the base block
            # before update() overwrites it, then merge per field.
            base_reasoning = base.get("reasoning")
            base.update(meta)
            if isinstance(meta.get("reasoning"), dict):
                base["reasoning"] = merge_reasoning(base_reasoning, meta["reasoning"])
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    out = []
    if r == "all":
        order = list(INTL_UI_ORDER) + [m for m in CN_UI_ORDER if m not in INTL_UI_ORDER]
    seen = set()
    for mid in order:
        if mid in merged and mid not in seen:
            seen.add(mid)
            out.append((mid, merged[mid]))
    if extras:
        # A model the curated table has never heard of still ships when the
        # *live* catalogue lists it - that is how a newly added upstream model
        # reaches /v1/models without a release. The bundled snapshot alone is
        # not enough: it also carries legacy entries the picker may not show.
        for mid, _meta in primary or []:
            if mid in merged and mid not in seen:
                seen.add(mid)
                out.append((mid, merged[mid]))
    return out


def fetch_endpoint_models(pool=None, realm="intl"):
    """Live menu, fetched from the SAME host the chat requests go to.

    The endpoint used to be hardwired to workbuddy.ai, which answers cn
    tokens too (and hands back the intl menu) — the cn realm then listed
    models the domestic servers do not actually serve. Pick a ready
    account of the requested realm instead and use its chat_base_url().
    """
    account = pool.pick(realm=realm) if pool else None
    if account is None:
        log("model discovery skipped: no usable %s account" % realm)
        cached = tr._models_cache.get(realm, {}).get("data")
        return [m for m, _ in (cached or [])]
    req = urllib.request.Request(account.chat_base_url() + MODELS_PATH,
                                 method="GET", headers=account.headers())
    try:
        with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log("model discovery failed (%s): %s" % (realm, exc))
        cached = tr._models_cache.get(realm, {}).get("data")
        return [m for m, _ in (cached or [])]
    ids, seen = [], set()
    data = payload.get("data") or {}
    for m in data.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else m
        if isinstance(mid, str) and mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    for agent in data.get("agents") or []:
        for mid in agent.get("models") or []:
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids


def remote_config_headers(account, realm, ua=None):
    """Headers for the product-config call.

    The UA decides which catalogue comes back and only the desktop UA returns
    the full list (an unknown one is a hard 400, code 12403), so this uses a
    realm's fixed desktop UA rather than the account's current identity.
    """
    cfg = wb_accounts.get_realm_config(realm)
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": ua or cfg["chat_ua"],
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
        "Authorization": "Bearer " + account.access_token,
        "X-User-Id": account.uid,
    }


def _agent_model_lists(payload):
    """Every agent's bare-string model list, cli-named agents first.

    The catalogue the picker shows rides in agents[].models. The endpoint
    answers with it under a "data" key while the desktop cache file is the
    same document written to disk without that envelope, so both are read.
    """
    roots = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        roots.append(data)
    cli, other = [], []
    for root in roots:
        agents = root.get("agents")
        if isinstance(agents, dict):
            entries = list(agents.items())
        elif isinstance(agents, list):
            entries = [((entry.get("name") if isinstance(entry, dict) else None),
                        entry) for entry in agents]
        else:
            continue
        for name, entry in entries:
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            if not (isinstance(models, list) and models
                    and isinstance(models[0], str)):
                continue
            ids = [str(m).strip() for m in models if isinstance(m, str)]
            ids = [m for m in ids if m]
            if not ids:
                continue
            (cli if str(name or "").strip().lower() == "cli" else other).append(ids)
    return cli, other


def parse_remote_catalog(payload):
    """(ids, meta) from a /v3/config response, or None when it carries none.

    The picker's list rides in agents[].models as bare ids - under "data" in
    the endpoint's answer, at the top level in the desktop cache file. The
    per-model metadata (credits, limits, copy) lives in a separate models
    array. An unusable credential answers HTTP 200 with an *empty* list, so
    an empty catalogue is reported as None and the caller falls back instead
    of publishing "this exit has no models".
    """
    if not isinstance(payload, dict):
        return None
    cli_lists, other_lists = _agent_model_lists(payload)
    pool = cli_lists or other_lists
    best = max(pool, key=len) if pool else None
    ids, seen = [], set()
    for mid in best or []:
        if mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if not ids:
        return None

    meta = {}

    def walk(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models \
                and isinstance(models[0], dict) and models[0].get("id"):
                for item in models:
                    mid = str(item.get("id") or "").strip()
                    if mid:
                        meta.setdefault(mid, item)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return ids, meta


def snapshot_credits():
    """id -> credits from the bundled catalogue (both realms, intl first).

    Used to answer "is there a free sibling?" for a variant the remote lists
    but whose sibling it no longer does: the free hy4-preview-f, for example,
    is what the cn picker keeps while the remote only names the paid one.
    """
    out = {}
    for source in (getattr(wb_catalog, "STATIC_INTL_MODELS", []),
                           getattr(wb_catalog, "STATIC_CN_MODELS", [])):
        for item in source or []:
            mid = str(item.get("id") or "").strip()
            if mid:
                out.setdefault(mid, str(item.get("credits") or "").strip().lower())
    return out


def curate_remote_catalog(realm, ids, meta=None):
    """Trim a remote catalogue to the models the picker should offer.

      - virtual aliases (default-model ... auto) are not models;
      - "-sg" / "-x" builds are the paid variant of a name the list already
        carries;
      - when a free ("x0.00") sibling exists, the free one is the one the
        picker shows, so the paid sibling is dropped;
      - everything else keeps its upstream order. Names the upstream does not
        list at all stay available through the curated order tables and the
        bundled snapshot, which merge_catalog() keeps.
    """
    credits = snapshot_credits()
    for mid, item in (meta or {}).items():
        if isinstance(item, dict):
            credits[mid] = str(item.get("credits") or "").strip().lower()
    order = CN_UI_ORDER if realm == "cn" else INTL_UI_ORDER
    known = set(ids) | set(credits) | set(order)

    def free(mid):
        return credits.get(mid) in ("x0.00", "x0", "0", "0.00")

    out = []
    for mid in ids:
        if not is_chat_model(mid):
            continue
        if mid.endswith(VARIANT_SUFFIXES):
            continue
        if mid.endswith("-f"):
            base = mid[:-2]
            if base in known and free(base) and not free(mid):
                continue
        elif (mid + "-f") in known and free(mid + "-f") and not free(mid):
            continue
        out.append(mid)
    return out


def fetch_remote_product_config(realm, pool=None):
    """(ids, meta) from the realm's own product-config endpoint, or None.

    At most two 10s attempts bound the wait: one per desktop UA, because the
    endpoint sits behind the WAF where a dropped connection is normal, and
    every caller has a fallback (the desktop cache file, the narrow model
    endpoint, the bundled snapshot).
    """
    if realm not in ("intl", "cn") or pool is None:
        return None
    account = pool.representative(realm=realm)
    if account is None or not account.access_token:
        log("remote catalog: no usable %s account, skipping" % realm)
        return None
    cfg = wb_accounts.get_realm_config(realm)
    url = cfg["chat_upstream"] + REMOTE_CONFIG_PATH
    # The chat UA is the desktop identity the rest of the gateway uses; the
    # plain app UA is the second try, for a build that answers only to it.
    uas = [cfg["chat_ua"]]
    if cfg.get("billing_ua") and cfg["billing_ua"] != cfg["chat_ua"]:
        uas.append(cfg["billing_ua"])
    last = None
    for ua in uas:
        headers = remote_config_headers(account, realm, ua)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with wb_accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            continue
        parsed = parse_remote_catalog(payload)
        if parsed:
            return parsed
        last = "empty catalogue"
    log("remote catalog: %s fetch failed (%s)" % (realm, last))
    return None


def curated_live_sources(realm, pool=None):
    """(entries, extras) for the realm's live catalogue, already curated.

    Remote /v3/config first; `extras` says the entries came from the desktop
    catalogue, whose membership may add a model the curated tables have never
    seen. The bundled snapshot is the last fallback (no desktop-app cache
    file: this port ships none).
    """
    remote = None
    try:
        remote = fetch_remote_product_config(realm, pool=pool)
    except Exception as exc:
        log("remote catalog: %s failed (%s)" % (realm, exc))
    if remote:
        ids, meta = remote
        return ([(mid, meta.get(mid) or {})
                 for mid in curate_remote_catalog(realm, ids, meta)], True)
    return [], False


def fetch_models(realm=None, pool=None):
    r = realm or get_realm()
    # One upstream walk per realm even when several callers miss the cache at
    # the same moment: a batch of /v1/models requests must not turn into a
    # batch of upstream requests.
    with _catalog_lock:
        with _state._lock:
            c = tr._models_cache.get(r) or {"at": 0.0, "data": None}
            if c["data"] and time.time() - c["at"] < 300:
                return c["data"]
        live, extras = [], False
        if r in ("intl", "cn"):
            live, extras = curated_live_sources(r, pool=pool)
            if not live:
                # The narrow endpoint is not the desktop catalogue, so it keeps
                # the old whitelist behaviour: only names the order table knows.
                live = [(m, {}) for m in fetch_endpoint_models(pool=pool, realm=r)]
                extras = False
        entries = merge_catalog(live, realm=r, extras=extras)
        with _state._lock:
            tr._models_cache[r] = {"at": time.time(), "data": entries}
        return entries


def fetch_models_all(pool=None):
    """Full menu across BOTH exits, independent of the global realm.

    A model appears exactly once under its bare id. Only models served by
    BOTH exits additionally get "cn:<id>" / "global:<id>" aliases — for an
    exclusive model the bare id already pins the exit, and listing a second
    name for it is duplicate-menu noise. Current-realm entries come first
    so the order still reflects the gateway default. Each entry's meta
    carries "exitRealm" (bare dual-exit entries: the current realm).
    """
    catalogs = {realm: dict(fetch_models(realm=realm, pool=pool))
                for realm in ("intl", "cn")}
    dual = set(catalogs["intl"]) & set(catalogs["cn"])
    out, seen = [], set()
    current = get_realm()
    if current not in ("intl", "cn"):
        current = "intl"
    for realm in (current, "cn" if current == "intl" else "intl"):
        for mid, meta in catalogs[realm].items():
            if mid in seen:
                continue
            seen.add(mid)
            m = dict(meta or {})
            m["exitRealm"] = realm
            out.append((mid, m))
            if mid in dual:
                for alt_realm in ("intl", "cn"):
                    alt = dict(catalogs[alt_realm][mid] or {})
                    alt["exitRealm"] = alt_realm
                    out.append((("global:" if alt_realm == "intl" else "cn:") + mid,
                                alt))
    return out


def model_entry(mid, meta):
    """Build a rich /v1/models entry from the desktop app catalog metadata.

    The OpenAI spec only names id/object/created/owned_by, so capability data
    is convention-driven. Several shapes are emitted at once so that different
    clients (OpenRouter-style, LobeChat-style, plain-flag readers) all find
    what they look for.
    """
    meta = meta or {}
    item = {
        "id": mid,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "workbuddy",
    }
    name = meta.get("name")
    if name:
        item["name"] = name
    desc = meta.get("descriptionEn") or meta.get("descriptionZh")
    if desc:
        item["description"] = desc
    # ---- modality / capability ----
    # disabledMultimodal explicitly turns image input off; absent means allowed.
    vision = bool(meta.get("supportsImages")) and not meta.get("disabledMultimodal")
    tools = bool(meta.get("supportsToolCall"))
    thinks = bool(meta.get("supportsReasoning"))
    inputs = ["text"] + (["image"] if vision else [])
    item["capabilities"] = {"vision": vision, "tool_calls": tools, "reasoning": thinks}
    item["supports_vision"] = vision
    item["supports_images"] = vision
    item["supports_tool_calls"] = tools
    item["supports_reasoning"] = thinks
    item["vision"] = vision
    item["multimodal"] = vision
    item["abilities"] = {"vision": vision, "functionCall": tools,
                         "function_call": tools, "reasoning": thinks}
    item["input_modalities"] = inputs
    item["output_modalities"] = ["text"]
    item["modalities"] = {"input": inputs, "output": ["text"]}
    item["architecture"] = {"input_modalities": inputs, "output_modalities": ["text"],
                            "modality": "+".join(inputs) + "->text"}
    # ---- limits ----
    if meta.get("maxInputTokens"):
        item["context_length"] = meta["maxInputTokens"]
        item["max_input_tokens"] = meta["maxInputTokens"]
    if meta.get("maxOutputTokens"):
        item["max_output_tokens"] = meta["maxOutputTokens"]
        item["max_completion_tokens"] = meta["maxOutputTokens"]
    ctx = (meta.get("contextWindow") or {}).get("supportedLengths")
    if ctx:
        item["context_windows"] = ctx
    # ---- reasoning controls ----
    reasoning = meta.get("reasoning") or {}
    efforts = reasoning.get("supportedEfforts")
    if efforts:
        item["reasoning_efforts"] = efforts
    if reasoning.get("effort"):
        item["reasoning_fixed_effort"] = reasoning["effort"]
    if reasoning.get("defaultEffort"):
        item["reasoning_default_effort"] = reasoning["defaultEffort"]
    if reasoning.get("canDisableThinking") is not None:
        item["reasoning_can_disable"] = reasoning["canDisableThinking"]
    # DeepSeek 4.1 official supports low / high / max
    if mid == "deepseek-v4.1-flash":
        item["reasoning_efforts"] = ["low", "high", "max"]
        item["reasoning_default_effort"] = "high"
        item.pop("reasoning_fixed_effort", None)
    if meta.get("onlyReasoning") is not None:
        item["always_reasoning"] = bool(meta.get("onlyReasoning"))
    # ---- misc ----
    if meta.get("credits"):
        item["credits"] = meta["credits"]
    if meta.get("vendor"):
        item["vendor"] = meta["vendor"]
    if meta.get("temperature") is not None:
        item["temperature"] = meta["temperature"]
    if meta.get("top_p") is not None:
        item["top_p"] = meta["top_p"]
    if meta.get("isDefault"):
        item["is_default"] = True
    tags = [t for t in (meta.get("tags") or [])
            if isinstance(t, str) and not t.startswith("badge:")]
    if tags:
        item["tags"] = tags
    return item
