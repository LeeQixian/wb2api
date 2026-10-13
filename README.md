# wb2api — 本地 WorkBuddy -> OpenAI 兼容网关（单人自用瘦身版）

从 workbuddy2api-hub 移植：无看板、无面板密码、无 Windows/桌面端凭据扫描、无局域网模式、
无多 Key 管理。只保留「起一个本地网关自己用」这条主干。纯 Python >=3.9 标准库，零依赖。

## 路由面（全部）

| 路由 | 鉴权 | 说明 |
|---|---|---|
| `GET /health` | 免 | 存活探针 + 池状态摘要 |
| `GET /realm` | 免 | 当前出口 |
| `POST /realm` | key* | 切换出口 intl/cn（落盘持久化） |
| `GET /v1/models` | 免 | 模型发现：默认吐双出口全量 + `cn:`/`global:` 别名条目；带 `X-Realm: intl/cn` 则只列该出口 |
| `GET /credits` | 免 | 各账号积分/余额 |
| `POST /v1/chat/completions` | key* | stream / 非 stream |
| `POST /v1/responses` | key* | Responses API（Codex / Claude Code） |

`*` 仅当配置了 API Key 时强制；没配置就全放行（默认只绑 127.0.0.1，方便本地调试）。
Key 只认 `Authorization: Bearer` / `x-api-key` / `api-key` 头，**不接受 `?key=`**
（会泄进浏览器历史与 Referer，故意删掉）。CORS 默认关闭，`--cors` 显式打开。

## 快速开始

```bash
cd ~/Projects/Utility/wb2api

# 1. 加账号（任选其一；token 也可带 --refresh 一起导入）
python3 -m gateway.wbctl login --realm intl     # 浏览器 OAuth，免客户端
python3 -m gateway.wbctl import /path/to/auth.json          # 桌面端/旧反代的 JSON（自动识别嵌套/扁平）
python3 -m gateway.wbctl import '<accessToken JWT>'          # 或直接喂 token
python3 -m gateway.wbctl import - --refresh <rt>             # 或 stdin
# 2. 起网关
python3 -m gateway.server                       # 127.0.0.1:8788
python3 -m gateway.server --api-key sk-local    # 顺手持久化 key
python3 -m gateway.server --tasks               # 打开签到/猫猫旅行后台调度（默认关）

# 3. 用
export OPENAI_API_BASE=http://127.0.0.1:8788/v1
export OPENAI_API_KEY=***
```

## 选出口（intl / cn）

优先级从高到低：

1. **模型名前缀**（只认冒号）：`cn:hy3` 强制国内版出口，`global:hy3` 强制国际版
   （`intl:` 同义）。上游只收到裸名，响应与 `usage.jsonl` 回显你请求的带前缀名。
   前缀 + 模型专属出口冲突会 400，不会偷偷换家。
2. **`X-Realm: cn|intl` 头或 `?realm=`**：同上，逐请求钉死。
3. **自动路由（啥都不写）**：
   - 单出口模型自己找家：`glm-5.3-flash`、`minimax-*`、`kimi-k3-1` 等 -> cn；
     `gpt-*`、`gemini-*` 等 -> intl。全局 realm 不影响它们。
   - 双出口模型（`hy3`、`deepseek-v4.1-flash`、`glm-5.3`…）-> 跟随全局 realm。
4. **全局默认 realm**：`wbctl realm cn` 或 `POST /realm`，落盘持久化。

`/v1/models` 的 `exitRealm` 字段标出每个条目属于哪个出口。

## 羊毛与守卫（默认关闭）

签到、成长任务、猫猫旅行、国际版每日活跃对话保留自 hub，但：
- `--tasks` / `scheduler` 默认关；手动 `wbctl checkin` / `wbctl daily-chat` 不受限
- 全部等待间隔随机化（2–7s，账号间 3–10s）
- 调度时刻每日 ±15~45 分钟随机偏移（当天落盘，重启不漂移）
- 国际版日活真正算数的是网页通道会话（hub issue #59/#75/#90：只建会话不接沙箱
  不计数），由 `daily-chat-web` 开关控制（默认开）

## 守卫开关（settings.json，`wbctl set-switch <name> [值]`，下一请求即生效）

| 开关 | 默认 | 作用 |
|---|---|---|
| `reserve-credits` | 0=关 | 余额 ≤ 阈值的账号停止派单，永不清零（清零会触发上游提醒短信） |
| `daily-token-limit` | 0=关 | 账号当日 token 用量（按 usage.jsonl 折算）达上限即停放至本地零点；全池被停时 429 + Retry-After |
| `auto-switch-product` | off | 429 时轮换出站身分（workbuddy→vscode→cli→…三条独立配额线）；也可 `wbctl product <uid> <名>` 手动钉死（落盘，重启保留，hub issue #76） |
| `daily-chat-web` | on | 国际版日活是否走网页通道 |
| `local-web-tools` | off | 网关自己执行 `web_search`/`web_fetch`（DuckDuckGo HTML，免 key）；开了即意味着网关会去抓模型要求的 URL |
| `expiring-window` | 7 天 | 临期积分优先派单（hub #174）：最近到期包落在窗口内的账号被优先使用，按紧迫度加权轮转（nginx SWRR，同天到期轮流）；0=纯轮转 |
| `credits-refresh` | 12 小时 | 后台积分刷新器的陈旧阈值（配合 #174 与 reserve）：每 tick 只刷最旧的一个账号（≤48 次计费调用/天），失败缓 6h，0=关；仅 `--tasks` 时运行 |
| `upstream.header/idle_timeout_seconds` | 120/300 | 上游 socket：首包超时与流中间每次读的空闲超时（hub #148）；卡死的流不再占住并发槽到 10 分钟 |

账号级失败治理（hub #163，无需开关、默认生效）：账号级 429 → 指数退避（10min→2h）；
连续 3 次硬失败（5xx）→ 熔断 30min→6h；连续 5 次未知失败 → 降权 10min→2h；一次成功
全部清零。402 余额不足 → 停放至次日本地 04:00（#157），余额刷新见 >0 即提前解冻。

系统提示词模式（hub #160，`wbctl set-prompt [passthrough|custom|append] [--file f]`）：
passthrough（默认，行为同旧版）＝保留客户 system，缺省时补中性占位；custom＝整段换成
网关提示词；append＝在客户 system 块后追加网关提示词。占位/网关文本由 `--system-prompt
[--system-prompt-file]` 或 `prompt.file` 决定。不带 hub 的 403 降级重发（本契约 403 直通）。

凭据文件不再保存 `lastError`/`cooldownUntil`（hub #144：早已恢复的限流标签会跨重启复活）；
`lastDailyChat` 照常落盘。Responses 请求不带 `max_output_tokens` 时按目录默认补 `max_tokens`
（#121，上游内部默认很低，长推理会被截到吐不出正文）。流被 max_tokens/断流截断时，半截
JSON 参数的 tool_call 直接丢弃而非发给客户解析不了的对象（#153）；并行 tool_call 批次被
客户端拆成相邻多条 assistant 的形态会在出站前合并回去（11148 的另一种触发形状）。

## 布局

每个分区一份 README.md 讲清职责与设计；同名顶层 .py 是门面，对外接口不变。

```
gateway/
  server.py main.py        # 入口与启动（python3 -m gateway.server）
  http/                    # HTTP 层：handler、路由、状态、usage 落盘
  protocol/                # OpenAI <-> WorkBuddy 协议转译（协议知识，勿顺手改）
  account/                 # Account / AccountPool / JWT / OAuth / 日活 / 退避熔断
  upstream/                # 出站请求 + 重试轮 / 429 / 403 / 402 + 连接池 + live 目录
  config/                  # settings.json、系统提示词模式、出站身分、指纹
  tasks/                   # 羊毛引擎（日常任务随 --tasks；成长任务走 wbctl growthplan）
  cli/                     # wbctl 命令行
  common/                  # 共享 log / paths / ids
  data/                    # 模型目录快照（catalog_intl.json / catalog_cn.json）
accounts/                  # 0700；token 明文 JSON 0600，勿同步
usage/                     # usage.jsonl 流水（每行含 at epoch，守卫据此统计）
tests/                     # python3 -m unittest discover -s tests
```

## 与 hub 版差异一览

同步基线与逐条同步记录见 `SYNC_PROGRESS.md`（当前基线：hub `4c6fab5` = v1.7.2）。

删除：dashboard.html、面板密码/PBKDF2/会话、多 API Key 与出口绑定、代理槽位、
桌面凭据扫描（%LOCALAPPDATA%）、.bat/.command、LAN 启动脚本、Docker、
用量看板统计（analytics/perf/by-account）、日志查看器、账号导出/导入看板流。
改动：open_upstream 的 POOL/log 参数化；?key= 移除；CORS 默认关；
出口指定从旧网关的模型名前缀改为 header/全局 realm 优先，同时兼容 `cn:`/`global:` 前缀；
`/v1/models` 默认返回双出口全量（hub 是按当前 realm 单列表）。
auth 从「面板或 key」简化为「可选单 key」；token 文件权限 0600/0700。

## 风险备忘

- `accounts/` 是明文 token，等同账号钥匙；备份/同步前自己加密。
- 上游风控真实存在：多号同 IP、高频并发、批量任务都会提高封号概率。低频自用 +
  默认关 tasks 是最稳姿态。
- 本工具不提供任何账号与额度，仅限个人合法自有账号私有环境使用。
