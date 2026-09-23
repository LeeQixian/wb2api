"""Runtime settings for the gateway: one optional API key.

`accounts/settings.json` holds a single field, {"api_key": "..."}.
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

SETTINGS_NAME = "settings.json"


def _path(directory):
    return os.path.join(directory, SETTINGS_NAME)


def load(directory):
    try:
        with open(_path(directory), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save(directory, data):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _path(directory))
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
