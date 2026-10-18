# wb2api — 本地 WorkBuddy -> OpenAI 兼容网关

把本机的 WorkBuddy 账号变成一个 OpenAI 兼容的本地接口：起一个进程，任何
OpenAI 客户端把 base URL 指到 `http://127.0.0.1:8788/v1` 即可用。

单人自用，纯 Python 标准库（Python >= 3.9），零依赖。

## 路由面（全部）

| 路由 | 鉴权 | 说明 |
|---|---|---|
| `GET /health` | 免 | 存活探针 + 池状态摘要 |
| `GET /realm` | 免 | 当前出口 |
| `POST /realm` | key* | 切换出口 intl/cn（落盘持久化） |
| `GET /v1/models` | 免 | 模型发现：默认吐双出口全量 + `cn:`/`global:` 别名；带 `X-Realm: intl/cn` 只列该出口 |
| `GET /credits` | 免 | 各账号积分/余额 |
| `POST /v1/chat/completions` | key* | stream / 非 stream |
| `POST /v1/responses` | key* | Responses API（Codex / Claude Code） |

`*` 仅当配置了 API Key 时强制；没配置就全放行（默认只绑 127.0.0.1）。
Key 只认 `Authorization: Bearer` / `x-api-key` / `api-key` 头，不接受 `?key=`
（会泄进浏览器历史与 Referer）。CORS 默认关闭，`--cors` 显式打开。

## 快速开始

```bash
cd ~/Projects/Utility/wb2api

# 1. 加账号（任选其一；token 也可带 --refresh 一起导入）
python3 -m gateway.wbctl login --realm intl     # 浏览器 OAuth
python3 -m gateway.wbctl import /path/to/auth.json   # 桌面端/旧反代的 JSON（自动识别）
python3 -m gateway.wbctl import '<accessToken JWT>'  # 或直接喂 token
python3 -m gateway.wbctl import - --refresh <rt>     # 或 stdin

# 2. 起网关
python3 -m gateway.server                       # 127.0.0.1:8788
python3 -m gateway.server --api-key sk-local    # 顺手持久化 key
python3 -m gateway.server --tasks               # 打开后台签到/保活调度（默认关）

# 3. 用
export OPENAI_API_BASE=http://127.0.0.1:8788/v1
export OPENAI_API_KEY=***
```

## 选出口（intl / cn）

优先级从高到低：

1. **模型名前缀**（只认冒号）：`cn:hy3` 强制国内版出口，`global:hy3` 强制国际版
   （`intl:` 同义）。上游只收到裸名，响应与 `usage.jsonl` 回显你请求的带前缀名。
   前缀 + 模型专属出口冲突会 400，不会偷偷换家。
2. **`X-Realm: cn|intl` 头或 `?realm=`**：逐请求钉死。
3. **自动路由（啥都不写）**：单出口模型自己找家；双出口模型跟随全局 realm。
4. **全局默认 realm**：`wbctl realm cn` 或 `POST /realm`，落盘持久化。

`/v1/models` 的 `exitRealm` 字段标出每个条目属于哪个出口。

## 后台调度（`--tasks`，默认关）

`--tasks` 打开后，逐账号执行：

- **Token 保活**：剩余寿命不足 2 小时自动刷新。
- **国内版**：每日签到、猫猫旅行、夜猫子任务。
- **国际版**：每日活跃对话（走网页通道才真正算数）。

保活、签到、旅行对**每个能跑通的账号**无条件执行，与 enable/disable 无关；
enable 只管这个账号要不要接推理请求、花它的积分。

一次性成长任务不在此列，用 `wbctl growthplan <uid>` 手动跑。

反风控：账号之间 3~10 秒随机间隔，排程时刻每日 ±15~45 分钟随机偏移。

## 守卫开关（settings.json，`wbctl set-switch <name> [值]`）

| 开关 | 默认 | 作用 |
|---|---|---|
| `reserve-credits` | 0=关 | 余额 ≤ 阈值的账号停止派单，永不清零 |
| `daily-token-limit` | 0=关 | 账号当日 token 用量达上限即停放至本地零点 |
| `auto-switch-product` | off | 429 时轮换出站身分（三条独立配额线） |
| `daily-chat-web` | on | 国际版日活是否走网页通道 |
| `local-web-tools` | off | 网关自己执行 `web_search`/`web_fetch` |
| `expiring-window` | 7 天 | 临期积分优先派单；0=纯轮转 |
| `credits-refresh` | 12 小时 | 后台积分刷新器的陈旧阈值，仅 `--tasks` 时运行 |
| `upstream.header/idle_timeout_seconds` | 120/300 | 上游首包超时与流空闲超时 |

账号级失败治理（默认生效）：账号级 429 指数退避（10min→2h）；连续硬失败触发
熔断（30min→6h）；402 余额不足停放至次日本地 04:00，余额恢复提前解冻。
所有连击计数都受时间窗约束（`WB_FAIL_WINDOW`，默认 600 秒）：隔得久了从第一档重算，
不会把昨天几次限流攒成今天的长冷却；冷却剩余时间按秒显示。

请求体上限 16MB（`WB_MAX_PAYLOAD_BYTES`），超限返回 413 并附 `gateway_hint`
说明该改哪个环境变量。GPT 家族模型的 `max_tokens` 低于 16 会被夹到 16（上游下限）。

每次请求结束会按上游回包的 `credit` 立即扣减内存里的余额估计，余额守卫不必等到
下一轮刷新才生效（`credits-refresh` 仍是后台刷新的陈旧阈值）。

系统提示词模式（`wbctl set-prompt [passthrough|custom|append] [--file f]`）：
passthrough（默认）保留客户 system，缺省时补中性占位；custom 整段换成网关提示词；
append 在客户 system 后追加。403 一律直通，不降级重发。

## 布局

每个分区一份 README.md；同名顶层 `.py` 是该分区的对外入口。

```
gateway/
  server.py main.py        # 入口与启动
  http/                    # HTTP 层：handler、路由、状态
  protocol/                # OpenAI <-> WorkBuddy 协议转译
  account/                 # 账号、池、凭据、退避熔断
  upstream/                # 出站请求、重试轮、连接池、live 目录
  config/                  # settings.json、提示词模式、出站身分
  tasks/                   # 后台任务与一次性成长任务
  cli/                     # wbctl 命令行
  common/                  # 共享 log / paths / ids
  data/                    # 模型目录快照
accounts/                  # 0700；token 明文 JSON 0600，勿同步
usage/                     # usage.jsonl 流水
tests/                     # python3 -m unittest discover -s tests
```

## 风险备忘

- `accounts/` 是明文 token，等同账号钥匙；备份/同步前自己加密。
- 上游风控真实存在：多号同 IP、高频并发、批量任务都会提高封号概率。低频自用 +
  默认关 tasks 是最稳姿态。
- 本工具不提供任何账号与额度，仅限个人合法自有账号私有环境使用。
