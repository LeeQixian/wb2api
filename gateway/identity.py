# -*- coding: utf-8 -*-
"""wb_identity.py - 出站身分（CLI 頭 / WorkBuddy 頭）

移植自 CangShui/buddy-proxy (BSD-3-Clause) internal/provider/client.go:497
BuildProtocolDirectHeaders()。原專案 Go 寫成，這裡改 Python 並接上本專案帳號模型。

為什麼要有這個模組：
  舊的 wb_accounts.headers() 只送 6 個跟身分有關的標頭，且值是自創的
  （X-Product=WorkBuddy、X-IDE-Type=WorkBuddy）—— 官方根本沒這種組合。

  官方有兩套身分，buddy-proxy 證實可切換：

    CLI 頭       X-IDE-Type: CLI      UA: CLI/<ver> CodeBuddy/<ver>
    WorkBuddy 頭 X-IDE-Type: VSCode   UA: VSCode/<ver> WorkBuddy/<ver>

  兩者共同點：X-Product 一律是 "SaaS"。

原專案記載的兩個坑：
  * 上游 /v3/config 拒絕純 "CLI/<ver>"（12403：UA 版本解析失敗），
    官方格式同時包含 CLI 與 CodeBuddy 兩段版本。
  * 官方 CLI 的 X-Conversation-ID 用大寫 UUID。
"""

import uuid

DEFAULT_IDE_VERSION = "2.117.2"
DEFAULT_WORKBUDDY_IDE_VERSION = "1.119.0"
DEFAULT_WORKBUDDY_PRODUCT_VERSION = "4.9.29177644"

PRODUCT_CLI = "cli"
PRODUCT_WORKBUDDY = "workbuddy"
VALID_PRODUCTS = (PRODUCT_CLI, PRODUCT_WORKBUDDY)


def normalize_product(value):
    """把各種寫法收斂成 'cli' 或 'workbuddy'。"""
    v = str(value or "").strip().lower()
    if v in ("workbuddy", "wb", "ide", "vscode"):
        return PRODUCT_WORKBUDDY
    return PRODUCT_CLI


_ENDPOINTS = {
    ("intl", PRODUCT_CLI): ("https://www.codebuddy.ai", "www.codebuddy.ai"),
    ("intl", PRODUCT_WORKBUDDY): ("https://www.workbuddy.ai", "www.workbuddy.ai"),
    ("cn", PRODUCT_CLI): ("https://copilot.tencent.com", "copilot.tencent.com"),
    ("cn", PRODUCT_WORKBUDDY): ("https://www.workbuddy.cn", "www.workbuddy.cn"),
}


def endpoint_for(realm, product):
    """回傳 (chat base URL, X-Domain)。身分換了，端點也要跟著換。"""
    key = ("cn" if realm == "cn" else "intl", normalize_product(product))
    return _ENDPOINTS[key]


def domain_for_realm(realm):
    """X-Domain 的值（舊介面，預設 CLI）。"""
    return endpoint_for(realm, PRODUCT_CLI)[1]


def build_identity_headers(product, realm, uid, token,
                           conversation_id=None, enterprise_id="",
                           tenant_id="", department=""):
    """回傳這一輪要用的身分標頭。product = 'cli' 或 'workbuddy'。"""
    product = normalize_product(product)
    ide_version = DEFAULT_IDE_VERSION

    headers = {
        "X-Agent-Intent": "craft",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "X-IDE-Version": ide_version,
        "X-Domain": endpoint_for(realm, product)[1],
        "User-Agent": "CLI/%s CodeBuddy/%s" % (ide_version, ide_version),
        "X-Product": "SaaS",
        "X-User-Id": str(uid or "anonymous"),
        "Authorization": "Bearer " + str(token or ""),
    }

    conv = str(conversation_id or "").strip() or str(uuid.uuid4()).upper()
    msg_id = uuid.uuid4().hex
    headers["X-Conversation-ID"] = conv
    headers["X-Conversation-Request-ID"] = uuid.uuid4().hex
    headers["X-Conversation-Message-ID"] = msg_id
    headers["X-Request-ID"] = msg_id

    if product == PRODUCT_WORKBUDDY:
        headers["X-IDE-Type"] = "VSCode"
        headers["X-IDE-Name"] = "VSCode"
        headers["X-IDE-Version"] = DEFAULT_WORKBUDDY_IDE_VERSION
        headers["X-Product-Version"] = DEFAULT_WORKBUDDY_PRODUCT_VERSION
        headers["X-Env-ID"] = "production"
        headers["User-Agent"] = "VSCode/%s WorkBuddy/%s" % (
            DEFAULT_WORKBUDDY_IDE_VERSION, DEFAULT_WORKBUDDY_PRODUCT_VERSION)

    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
        headers["X-Tenant-Id"] = str(tenant_id or enterprise_id)
    if department:
        headers["X-Department-Info"] = str(department)

    return headers
