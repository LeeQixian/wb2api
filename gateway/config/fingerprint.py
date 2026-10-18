"""Device fingerprint derivation: ids per (uid + salt), stable and distinct
across accounts; never process-level constants (they would link accounts)."""
import hashlib
import time
import uuid


def derive_id(uid: str, salt: str) -> str:
    """Stable hyphenated UUIDv4-shaped id from uid + salt."""
    seed = f"{salt}:{uid or 'anonymous'}"
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()
    h = h[:12] + "4" + h[13:16] + "a" + h[17:]
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def session_id(uid: str) -> str:
    """Per-account sessionId; must never become a process constant."""
    return derive_id(uid, "session")


def generate_request_id(uid: str = "") -> str:
    """X-Request-ID: random 32-hex, not account-derived (5.8.4 shape)."""
    return uuid.uuid4().hex


def get_desktop_fingerprint(uid: str, nickname: str = "", os_name: str = "win32") -> dict:
    """Desktop fingerprint payload for activity reporting."""
    now = int(time.time() * 1000)
    return {
        "timezone": "Asia/Shanghai",
        "reportDelay": 2000,
        "userId": uid,
        "username": nickname,
        "userNickname": nickname,
        "product": "SaaS",
        "releaseDate": 1789036585355,
        "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
        "ideName": "WorkBuddy",
        "ideType": "WorkBuddy",
        "ideVersion": "5.5.6",
        "machineId": derive_id(uid, "machine"),
        "sessionId": derive_id(uid, "session"),
        "extName": "workbuddy-desktop",
        "extVersion": "5.5.6",
        "os": os_name,
        "arch": "x64",
        "osVersion": "10.0.26220",
        "cpuCores": 20,
        "memorySize": 24,
        "timestamp": now,
        "presentAt": now,
    }
