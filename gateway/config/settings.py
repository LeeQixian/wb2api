"""Runtime settings: optional API key plus panel switches (accounts/settings.json).
No key lets every request through; with one, /v1 routes need the Bearer header."""
import hmac
import json
import os
import tempfile
import threading

SETTINGS_NAME = "settings.json"
_load_lock = threading.Lock()
# (path, mtime, size) -> parsed dict; save() invalidates explicitly
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
    Timing-safe compare; an empty supplied value never touches the digest."""
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
    """Global low-credit guard: accounts at or below this balance stay idle.
    Zero disables the guard, as on installs that predate the setting."""
    return _positive_int(directory, "reserve_credits")


def set_reserve_credits(directory, value):
    """Persist the guard threshold. Returns the stored value."""
    return _set_positive_int(directory, "reserve_credits", value)


def daily_token_limit(directory):
    """Global daily guard: an account burning this many tokens stays idle.
    Zero disables the guard, as on installs that predate the setting."""
    return _positive_int(directory, "daily_token_limit")


def set_daily_token_limit(directory, value):
    """Persist the daily token threshold. Returns the stored value."""
    return _set_positive_int(directory, "daily_token_limit", value)


def auto_switch_product(directory):
    """Whether an upstream 429 may rotate an account's outbound identity.
    Off by default; rotation spends the retry budget on an unchosen channel."""
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
    On by default: only a web conversation registers the daily activity."""
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
    Off by default: enabling it makes the gateway fetch URLs models ask for."""
    return load(directory).get("local_web_tools") is True


def set_local_web_tools(directory, enabled):
    """Persist the local web-tools switch. Returns the stored boolean."""
    enabled = bool(enabled)
    data = load(directory)
    data["local_web_tools"] = enabled
    save(directory, data)
    return enabled


def upstream_timeouts(directory):
    """(header, idle) seconds for the chat upstream socket.
    Defaults 120s/300s; the key layout matches the hub's `upstream` group."""
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
    """Gateway system-prompt mode (default passthrough).
    mode: passthrough|custom|append; an empty file means the built-in prompt."""
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
    """Days inside which soonest-expiring credits make an account go first.
    Default 7; 0 disables the preference and dispatch stays round-robin."""
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
    """Hours a credit balance may go stale before the refresher updates it.
    Zero disables the refresher; garbage falls back to the default."""
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
