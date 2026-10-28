# wb2api

把一个 WorkBuddy 账号变成 OpenAI 兼容接口：本机跑一个网关进程，任何 OpenAI 客户端
把 base URL 指到 `http://127.0.0.1:8788/v1` 即可使用。纯 Python 标准库（>= 3.9），
无第三方依赖；数据只落在 `accounts/`（凭据）与 `usage/`（用量流水）两个目录。
精简自 [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，
面向单机自用：国区、海外区各自同时只启用一个账号。

## 快速开始

```bash
cd ~/Projects/Utility/wb2api

# 1. 加账号（浏览器登录，或导入已有凭据）
python3 -m gateway.wbctl login --realm intl
python3 -m gateway.wbctl import /path/to/auth.json

# 2. 确认账号就绪（ready=y）
python3 -m gateway.wbctl list

# 3. 起网关
python3 -m gateway.server            # 127.0.0.1:8788
python3 -m gateway.server --tasks    # 同时开启后台签到/保活调度（默认关）

# 4. 用
export OPENAI_API_BASE=http://127.0.0.1:8788/v1
export OPENAI_API_KEY=***
```

## 路由

| 路由 | 鉴权 | 说明 |
|---|---|---|
| `GET /health` | 免 | 存活探针与池状态：`accounts_by_realm`、`accounts_ready_by_realm`、`accounts_ready_current_realm`、`models_ready` |
| `GET /realm` | 免 | 当前默认出口 |
| `POST /realm` | key | 切换默认出口（`intl` / `cn`），落盘 |
| `GET /v1/models` | 免 | 双出口全量模型，带 `cn:` / `global:` 别名 |
| `GET /credits` | 免 | 各账号余额 |
| `POST /v1/chat/completions` | key | Chat Completions，流式或非流式 |
| `POST /v1/responses` | key | Responses API |
| `POST /v1/responses/compact` | key | Codex 远程上下文压缩 |

只有 `settings.json` 配了 `api_key` 才校验鉴权；默认只绑 `127.0.0.1`，本机全放行。
鉴权只认 `Authorization: Bearer`、`x-api-key`、`api-key` 三种头，不支持 `?key=`。
CORS 默认关，`--cors` 打开。

## 出口选择

出口指账号所属区域：`intl`（国际版）或 `cn`（国内版）。优先级从高到低：

1. 模型名前缀：`cn:hy3`、`global:hy3`（`intl:` 同义）。上游只收到裸名，响应与
   `usage.jsonl` 保留你请求的带前缀名；前缀与模型专属出口冲突时返回 400。
2. 请求头 `X-Realm: cn|intl` 或查询参数 `?realm=`。
3. 自动路由：单出口模型去自己的出口，双出口模型跟随默认出口。
4. 默认出口：`wbctl realm cn` 或 `POST /realm`，重启后保持。

`/v1/models` 的 `exitRealm` 字段标出每个模型属于哪个出口。

## 启动参数

| 参数 | 作用 |
|---|---|
| `--host` / `--port` | 监听地址与端口，默认 `127.0.0.1:8788` |
| `--api-key` | 持久化 API key；传空字符串则清除 |
| `--realm intl\|cn` | 切换默认出口并落盘 |
| `--tasks` | 后台调度：token 保活、国内签到、猫猫旅行、夜猫子、国际版日活 |
| `--cors` | API 路由允许任意来源跨域 |
| `--hide` | 忽略 kill/pkill 类信号，由 systemd 经 `SIGRTMIN+4` 停止 |
| `--accounts-dir` / `--usage-dir` | 凭据目录、用量目录 |
| `--system-prompt` / `--system-prompt-file` | 系统提示词占位文本或文本文件 |

## 设置（`accounts/settings.json`）

带名字的开关用 `wbctl set-switch <名字> [值]`；`upstream.*`、`schedule.*`、
`prompt.*` 直接编辑文件（`prompt` 也可用 `wbctl set-prompt`）。

| 字段 | 默认 | 作用 |
|---|---|---|
| `reserve-credits` | 0=关 | 余额不高于该值的账号停止派单 |
| `daily-token-limit` | 0=关 | 单账号当日 token 用量上限，到顶停放到次日零点 |
| `auto-switch-product` | off | 429 时轮换出站身份（三条独立配额线） |
| `daily-chat-web` | on | 国际版日活是否走网页通道 |
| `local-web-tools` | off | 网关自己执行 `web_search` / `web_fetch` |
| `expiring-window` | 7 天 | 临期积分优先派单；0=纯轮转 |
| `credits-refresh` | 12 小时 | 后台积分刷新阈值 |
| `upstream.header_timeout_seconds` / `idle_timeout_seconds` | 120 / 300 | 首包超时、流空闲超时 |
| `schedule.{checkin,travel,keepalive,cat}` | 9/21 · 9/21 · 22 · 1 | 后台排程时段，热加载 |
| `prompt.mode` / `prompt.file` | passthrough | 系统提示词模式（passthrough / custom / append） |
| `model_aliases` | 空 | 入口名映射：`{"别名":"真实模型"}` 或 `[{"from","to"}]` |
| `account_max_concurrent` | 0=不限 | 单账号并发 chat 请求上限（1..10） |
| `affinity_stable_key_enabled` | false | 会话粘滞键额外覆盖首条 assistant 回复 |

## 环境变量

| 变量 | 作用 |
|---|---|
| `WB_CA_BUNDLE` / `SSL_CERT_FILE` | 追加私有根证书（公司代理、TLS 解密） |
| `WB_KEEPALIVE_LEAD_HOURS` | token 提前续期的小时数，默认 48 |
| `WB_DEFAULT_REASONING_EFFORT` | 客户端未指定时的思考档位：`high` / `catalog` / `off` |
| `WB_MODEL_SCOPED_ERRORS_PENALIZE=1` | 模型级 5xx 计入账号熔断 |
| `WB_PRESERVE_REQUEST_CONTRACT=1` | 客户端数据原样透传，不做消毒与字段映射 |
| `WB_DESKTOP_CHAT_COMPAT=1` | 桌面身份 chat 改用 CLI/SDK 头族，默认关 |
| `WB_MAX_PAYLOAD_BYTES` | 请求体上限，默认 16MB |

## 行为要点

- 429 指数退避 10 分钟到 2 小时；连续硬失败熔断 30 分钟到 6 小时；402 余额不足停放
  至次日本地 04:00，余额恢复提前解冻。连击计数受 `WB_FAIL_WINDOW`（默认 600 秒）
  时间窗约束，超窗从第一档重算。
- 模型级 429 的重置时刻写入账号 JSON（`modelWindows`），重启后未到期的冷却继续生效；
  到点自动放行，成功请求即时清除。上游 `code 6004`（额度用满）的窗口额外带
  `cap` / `capAt`，与临时限流区分。
- 上游 `403 / code 11140`（账号级风控）写入 `restrictedUntil` 并停用该号 2 小时，
  重启与换身分都不清除；日活打卡同样跳过限制窗口内的账号，只有真被上游服务成功
  的一次请求才解除。
- 国际版日活网页会话登记在进程内（上限 128 条）；收尾失败或线程卡死时，调度器每
  60 秒代为关闭，避免占满上游 10 个活跃名额。
- `enable` / `disable` 只影响推理派单；`--tasks` 的保活、签到、旅行对每个能跑通的
  账号都会执行。一次性成长任务用 `wbctl growthplan <uid>` 手动跑。
- 账号间 3 到 10 秒随机间隔，排程时刻每日 ±15 到 45 分钟偏移，出站头与请求体对齐
  官方客户端。多号同 IP、高频并发、批量任务都会提高封号概率。
- `accounts/` 里是明文 token，等同账号钥匙，备份前自行加密。
- 本工具不提供账号与额度，仅限个人合法自有账号的私有环境使用。

## 目录

```
gateway/
  http/ protocol/ account/ upstream/ config/ tasks/ cli/ common/ data/
accounts/   # 0700；凭据 JSON 0600
usage/      # usage.jsonl 用量流水
tests/      # python3 -m unittest discover -s tests
```

每个分区一份 `README.md` 说明职责，同名顶层 `.py` 是该分区的对外入口。
命令行细节见 [USAGE.md](USAGE.md)。
