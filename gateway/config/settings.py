"""Runtime settings for the gateway: one optional API key + guard switches.

`accounts/settings.json` holds {"api_key": "..."} plus the operator switches
ported from the hub panel:

  reserve_credits      low-credit guard (0 = off)
  daily_token_limit    per-account daily token budget (0 = off)
  auto_switch_product  429 identity rotation (off unless turned on)
  daily_chat_web       intl web-channel activity step (on unless turned off)
  local_web_tools      gateway-side web_search/web_fetch (off, opt-in)

Auth semantics (deliberate, per owner's request):

  - no key configured  -> every request passes (local debugging mode)
  - key configured     -> /v1 inference routes require it as a Bearer header

Comparison uses hmac.compare_digest. The panel password / multi-key /
PBKDF2 machinery of the original hub project is gone: there is no panel.
"""
import hmac
import json
import os
import tempfile
import threading

SETTINGS_NAME = "settings.json"
_load_lock = threading.Lock()
# (path, mtime, size) -> parsed dict; save() invalidates explicitly (hub 8bc1f0d)
_load_cache = {"key": None, "value": {}}


def _path(directory):
    return os.path.join(directory, SETTINGS_NAME)


def load(directory):
    path = _path(directory)
    try:
        info = os.stat(path)
        key = (path, info.st_mtime, info.st_size)
    except OSError:
        key = (path, None, None)
    with _load_lock:
        if _load_cache["key"] == key:
            return dict(_load_cache["value"])
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            parsed = json.load(fh)
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        pass
    with _load_lock:
        _load_cache.update({"key": key, "value": data})
    return dict(data)


def save(directory, data):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _path(directory))
        with _load_lock:
            _load_cache["key"] = None
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def get_api_key(directory):
    key = str(load(directory).get("api_key") or "").strip()
    return key or None


def set_api_key(directory, key):
    key = str(key or "").strip()
    if not key:
        raise ValueError("api key must not be empty")
    data = load(directory)
    data["api_key"] = key
    save(directory, data)
    return key


def clear_api_key(directory):
    data = load(directory)
    data.pop("api_key", None)
    save(directory, data)


def auth_required(directory):
    return bool(get_api_key(directory))


def check_key(directory, supplied):
    """True when the request may proceed: no key configured, or a match.

    Timing-safe comparison; an empty `supplied` against a configured key is
    rejected without touching the digest.
    """
    expected = get_api_key(directory)
    if not expected:
        return True
    if not supplied:
        return False
    return hmac.compare_digest(str(supplied), expected)


def _positive_int(directory, field):
    """Read one guard threshold; zero/negative/garbage all mean "off"."""
    try:
        value = int(load(directory).get(field) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _set_positive_int(directory, field, value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    data = load(directory)
    data[field] = value
    save(directory, data)
    return value


def reserve_credits(directory):
    """Global low-credit guard: an account at or below this balance stays idle.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    return _positive_int(directory, "reserve_credits")


def set_reserve_credits(directory, value):
    """Persist the guard threshold. Returns the stored value."""
    return _set_positive_int(directory, "reserve_credits", value)


def daily_token_limit(directory):
    """Global daily guard: an account that already burned this many tokens
    today stays idle until local midnight.

    Zero disables the guard, which keeps installs that predate the setting
    behaving exactly as before.
    """
    return _positive_int(directory, "daily_token_limit")


def set_daily_token_limit(directory, value):
    """Persist the daily token threshold. Returns the stored value."""
    return _set_positive_int(directory, "daily_token_limit", value)


def auto_switch_product(directory):
    """Whether an upstream 429 may rotate an account's outbound identity.

    Off unless the operator turns it on. Rotating identity spends the request's
    retry budget and leaves the account on a channel nobody picked, so the
    gateway does not decide that on its own - and an install that predates the
    setting keeps behaving exactly as it did.
    """
    return load(directory).get("auto_switch_product") is True


def set_auto_switch_product(directory, enabled):
    """Persist the auto-switch toggle. Returns the stored boolean."""
    enabled = bool(enabled)
    data = load(directory)
    data["auto_switch_product"] = enabled
    save(directory, data)
    return enabled


def daily_chat_web(directory):
    """Whether the intl daily check-in also opens a web-channel conversation.

    On unless the operator turns it off: the desktop-identity chat completion
    this automation used to send does not register the daily activity, while a
    web conversation does (issues #75, #59, #90). An install that never touched
    the setting keeps the web step, because that is the behaviour that earns
    the credits.
    """
    value = load(directory).get("daily_chat_web")
    return True if value is None else value is True


def set_daily_chat_web(directory, enabled):
    """Persist the web-channel toggle. Returns the stored boolean."""
    enabled = bool(enabled)
    data = load(directory)
    data["daily_chat_web"] = enabled
    save(directory, data)
    return enabled


def local_web_tools(directory):
    """Whether the gateway runs web_search / web_fetch calls itself.

    Off unless the operator turns it on. Forwarding the client's declaration
    untouched is what this gateway has done since the hub's v1.5.3 revert, and
    it is what an install that never touched the switch keeps doing. Turning
    the switch on swaps the declaration for the gateway's own function and
    executes the calls locally (gateway.webtools), which also means the gateway
    itself fetches the URLs a model asks for - hence opt-in only.
    """
    return load(directory).get("local_web_tools") is True


def set_local_web_tools(directory, enabled):
    """Persist the local web-tools switch. Returns the stored boolean."""
    enabled = bool(enabled)
    data = load(directory)
    data["local_web_tools"] = enabled
    save(directory, data)
    return enabled


def upstream_timeouts(directory):
    """(header, idle) seconds for the chat upstream socket (hub #148).

    header covers connect/TLS/first byte; idle bounds each mid-stream read,
    so an active stream stays alive while a silent one fails out early and
    releases its concurrency slot. Defaults match the hub (120s/300s) and the
    key layout matches the hub's `upstream` group, so a settings.json stays
    compatible with a hub install. An entry that never touched the setting
    behaves exactly like before.
    """
    cfg = load(directory).get("upstream")
    cfg = cfg if isinstance(cfg, dict) else {}
    header, idle = 120.0, 300.0
    try:
        header = float(cfg.get("header_timeout_seconds") or header)
    except (TypeError, ValueError):
        pass
    try:
        idle = float(cfg.get("idle_timeout_seconds") or idle)
    except (TypeError, ValueError):
        pass
    return max(1.0, header), max(1.0, idle)


def set_upstream_timeouts(directory, header_seconds, idle_seconds):
    """Persist both timeouts; each accepts an int/float of seconds."""
    data = load(directory)
    group = data.get("upstream")
    group = group if isinstance(group, dict) else {}
    group["header_timeout_seconds"] = max(1, int(float(header_seconds)))
    group["idle_timeout_seconds"] = max(1, int(float(idle_seconds)))
    data["upstream"] = group
    save(directory, data)
    return group["header_timeout_seconds"], group["idle_timeout_seconds"]


def prompt_config(directory):
    """Gateway system-prompt mode (hub #160; default passthrough = old behaviour).

    mode: passthrough | custom | append; file: path to the gateway prompt text
    (empty = built-in default). Any garbage falls back to the defaults so a
    hand-edited settings.json cannot alter outbound traffic by accident.
    """
    stored = load(directory).get("prompt")
    stored = stored if isinstance(stored, dict) else {}
    mode = stored.get("mode")
    if not isinstance(mode, str) or mode.strip().lower() not in (
            "passthrough", "custom", "append"):
        mode = "passthrough"
    path_value = stored.get("file")
    if not isinstance(path_value, str):
        path_value = ""
    return {"mode": mode.strip().lower(), "file": path_value.strip()}


def set_prompt_config(directory, mode=None, file=None):
    """Persist one or both prompt fields; unknown mode values are refused."""
    data = load(directory)
    current = data.get("prompt")
    current = current if isinstance(current, dict) else {}
    if mode is not None:
        mode = str(mode).strip().lower()
        if mode not in ("passthrough", "custom", "append"):
            raise ValueError("prompt mode must be passthrough, custom or append")
        current["mode"] = mode
    if file is not None:
        current["file"] = str(file).strip()
    data["prompt"] = current
    save(directory, data)
    return prompt_config(directory)


def expiring_window_days(directory):
    """Window, in days, inside which an account's soonest-expiring credit
    package makes the pool hand that account out first, so credits about to
    lapse are spent before they are lost (hub #174).

    The shipped default is 7 days; 0 disables the preference and dispatch
    stays a plain round-robin.
    """
    try:
        value = int(load(directory).get("expiring_window_days"))
    except (TypeError, ValueError):
        return 7
    return value if value > 0 else 0


def set_expiring_window_days(directory, value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    data = load(directory)
    data["expiring_window_days"] = value
    save(directory, data)
    return value


def credits_refresh_hours(directory):
    """How stale a credit balance may get before the background refresher
    updates it, in hours (hub #174/#163 support).

    The dispatch preference reads the balance, and only the check-in /
    daily-activity paths used to refresh it, so an account could be judged on
    a balance days old. Zero disables the refresher; garbage falls back to
    the default so a hand-edited settings.json cannot wedge the loop.
    """
    raw = load(directory).get("credits_refresh_hours")
    if raw is None:
        return 12.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 12.0
    if value <= 0:
        return 0.0
    return min(value, 24 * 30)


def set_credits_refresh_hours(directory, value):
    try:
        value = max(0.0, float(value))
    except (TypeError, ValueError):
        value = 12.0
    data = load(directory)
    data["credits_refresh_hours"] = value
    save(directory, data)
    return value
