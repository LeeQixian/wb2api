# -*- coding: utf-8 -*-
"""Gateway-owned system-prompt modes (settings.json prompt.mode).

Rewrites the outbound system/developer message before it leaves, dropping
client-side prompt fingerprints that upstream content review matches. Only
system/developer is touched; user/assistant/tool are left to the sanitizer.

Modes, default passthrough:
  passthrough  keep the client system/developer; add a neutral placeholder
               when the client sends none (the upstream requires one)
  custom       drop every system/developer and use the gateway prompt
  append       insert the gateway prompt after the leading system/developer run

Deliberately omits the hub's 403 degrade-retry (degrade.go): this project's
contract is 403 pass-through, so retrying with a rewritten prompt is not ours.
"""

# Built-in prompt for custom/append; override with prompt.file.
DEFAULT_PROMPT = """你是一名工程助手，幫助用戶完成軟件工程任務。

- 先理解代碼與上下文再動手，遵循既有模式與約定。
- 最小改動：只改必要部分，不做無關重構。
- 改動後要驗證：跑測試或構建確認結果，不假設「應該沒問題」。
- 對不確定的事保持誠實：說明不確定並給出驗證路徑，不編造。
- 跟隨用戶語言；簡潔直接，技術術語精確。
- 如實報告失敗與邊界，不掩蓋、不粉飾。
"""

VALID_MODES = ("passthrough", "custom", "append")


def normalize_mode(value):
    """未知模式回退 passthrough（保守默認，不意外改寫客戶端提示詞）。"""
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_MODES else "passthrough"


def load_prompt(mode, file_path=""):
    """讀取網關提示詞：file 非空則讀檔（失敗拋出，調用方 fail open），否則內置默認。"""
    path = str(file_path or "").strip()
    if not path:
        return DEFAULT_PROMPT
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def rewrite(messages, system_prompt):
    """刪除所有 system/developer，頭部插入一條網關 system 訊息。"""
    if not system_prompt:
        return messages
    kept = [m for m in (messages or [])
            if not (isinstance(m, dict)
                    and m.get("role") in ("system", "developer"))]
    return [{"role": "system", "content": system_prompt}] + kept


def append(messages, system_prompt):
    """在開頭連續 system/developer 區塊之後插入網關提示詞，其餘訊息不動。"""
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
    """按模式路由一次請求的 messages。

    placeholder: passthrough 下客戶端沒帶 system 時補的中性占位
    （translate.SYSTEM_PROMPT）。上游要求請求含 system 訊息，這一條不能省；
    custom / append 由網關提示詞自己佔位，不需要它。
    """
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
