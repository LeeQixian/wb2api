# wb2api — 本地 WorkBuddy -> OpenAI 兼容网关（单人自用瘦身版）

从 workbuddy2api-hub 移植：无看板、无面板密码、无 Windows/桌面端凭据扫描、无局域网模式、
无多 Key 管理。只保留「起一个本地网关自己用」这条主干。纯 Python >=3.9 标准库，零依赖。

## 路由面（全部）

| 路由 | 鉴权 | 说明 |
|---|---|---|
| `GET /health` | 免 | 存活探针 + 池状态摘要 |
| `GET /realm` | 免 | 当前出口 |
| `POST /realm` | key* | 切换出口 intl/cn（落盘持久化） |
| `GET /v1/models` | 免 | 模型发现（OpenAI 客户端需要） |
| `GET /credits` | 免 | 各账号积分/余额 |
| `POST /v1/chat/completions` | key* | stream / 非 stream |
| `POST /v1/responses` | key* | Responses API（Codex / Claude Code） |

`*` 仅当配置了 API Key 时强制；没配置就全放行（默认只绑 127.0.0.1，方便本地调试）。
Key 只认 `Authorization: Bearer` / `x-api-key` / `api-key` 头，**不接受 `?key=`**
（会泄进浏览器历史与 Referer，故意删掉）。CORS 默认关闭，`--cors` 显式打开。

## 快速开始

```bash
cd ~/Projects/Utility/wb2api

# 1. 加账号（三选一）
python3 -m gateway.wbctl login --realm intl     # 浏览器 OAuth，免客户端
python3 -m gateway.wbctl import --token '<jwt>' # 直接喂 accessToken
#    或把 hub 那边的 accounts/*.json 拷过来（同格式）

# 2. 起网关
python3 -m gateway.server                       # 127.0.0.1:8788
python3 -m gateway.server --api-key sk-local    # 顺手持久化 key
python3 -m gateway.server --tasks               # 打开签到/猫猫旅行后台调度（默认关）

# 3. 用
export OPENAI_API_BASE=http://127.0.0.1:8788/v1
export OPENAI_API_KEY=***
```

## 国内版羊毛（默认关闭）

签到、成长任务、猫猫旅行保留自 hub，但：
- `--tasks` / `scheduler` 默认关；手动 `wbctl checkin` 不受限
- 全部等待间隔随机化（2–7s，账号间 3–10s）
- 调度时刻每日 ±15~45 分钟随机偏移（当天落盘，重启不漂移）

## 布局

```
gateway/
  server.py       # 唯一入口：HTTP + 路由（本文件表格即全部攻击面）
  upstream.py     # 池轮换 / 429 模型级冷却 / 403 直通 / 产品切换 / realm
  translate.py    # OpenAI <-> WorkBuddy 协议转译（协议知识，勿顺手改）
  accounts.py     # Account / AccountPool / JWT / OAuth 设备流
  settings.py     # accounts/settings.json：单一 api_key
  wbctl.py        # CLI：login/import/list/credits/checkin/set-key/...
  tasks.py scheduler.py  # 羊毛引擎（默认关）
  catalog.py fingerprint.py identity.py
accounts/         # 0700；token 明文 JSON 0600，勿同步
usage/            # usage.jsonl 流水
tests/            # python3 -m unittest discover -s tests
```

## 与 hub 版差异一览

删除：dashboard.html、面板密码/PBKDF2/会话、多 API Key 与出口绑定、代理槽位、
桌面凭据扫描（%LOCALAPPDATA%）、.bat/.command、LAN 启动脚本、Docker、
用量看板统计（analytics/perf/by-account）、日志查看器、账号导出/导入看板流。
改动：open_upstream 的 POOL/log 参数化；?key= 移除；CORS 默认关；
auth 从「面板或 key」简化为「可选单 key」；token 文件权限 0600/0700。

## 风险备忘

- `accounts/` 是明文 token，等同账号钥匙；备份/同步前自己加密。
- 上游风控真实存在：多号同 IP、高频并发、批量任务都会提高封号概率。低频自用 +
  默认关 tasks 是最稳姿态。
- 本工具不提供任何账号与额度，仅限个人合法自有账号私有环境使用。
