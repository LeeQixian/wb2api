# -*- coding: utf-8 -*-
"""Gateway system-prompt modes: passthrough, custom (replace) or append (insert after)."""

# Built-in prompt for custom/append; override with prompt.file.
DEFAULT_PROMPT = """你是一名工程助手，幫助用戶完成軟件工程任務。

- 先理解代碼與上下文再動手，遵循既有模式與約定。
- 最小改動：只改必要部分，不做無關重構。
- 改動後要驗證：跑測試或構建確認結果，不假設「應該沒問題」。
- 對不確定的事保持誠實：說明不確定並給出驗證路徑，不編造。
- 跟隨用戶語言；簡潔直接，技術術語精確。
- 如實報告失敗與邊界，不掩蓋、不粉飾。
"""

# passthrough keeps the client's system/developer messages untouched.
VALID_MODES = ("passthrough", "custom", "append")


def normalize_mode(value):
    """Unknown modes fall back to passthrough, never rewriting the prompt."""
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_MODES else "passthrough"


def load_prompt(mode, file_path=""):
    """Gateway prompt: read `file` when set (raises on failure), else default."""
    path = str(file_path or "").strip()
    if not path:
        return DEFAULT_PROMPT
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def rewrite(messages, system_prompt):
    """Drop every system/developer message and prepend the gateway one."""
    if not system_prompt:
        return messages
    kept = [m for m in (messages or [])
            if not (isinstance(m, dict)
                    and m.get("role") in ("system", "developer"))]
    return [{"role": "system", "content": system_prompt}] + kept


def append(messages, system_prompt):
    """Insert the gateway prompt after the leading system/developer run."""
    if not system_prompt:
        return messages
    msgs = list(messages or [])
    insert_at = 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("system", "developer"):
            break
        insert_at += 1
    return (msgs[:insert_at]
            + [{"role": "system", "content": system_prompt}]
            + msgs[insert_at:])


def apply_mode(messages, mode, text, placeholder=None):
    """Route one request's messages by mode; returns the new message list.
    placeholder fills in for passthrough when the client sent no system."""
    mode = normalize_mode(mode)
    if mode == "custom":
        return rewrite(messages, text)
    if mode == "append":
        return append(messages, text)
    if messages and isinstance(messages[0], dict) \
            and messages[0].get("role") == "system":
        return messages
    if not placeholder:
        return messages
    return [{"role": "system", "content": placeholder}] + list(messages or [])
