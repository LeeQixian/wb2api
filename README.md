# wb2api — 本地 WorkBuddy -> OpenAI 兼容网关

## 来历

上游是 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)。
本项目是它的单人本地瘦身分叉，只跟进上游已发布的 tag，同步基线见 `SYNC_PROGRESS.md`。

## 为什么改造

上游是「面板 + 多号池 + 容器发布」的一体化部署包。本机不需要面板，也不需要
多账号协调调度，只要一个能挂在本地、给现成 OpenAI 客户端当上游的网关：

- 砍掉看板、i18n、Docker/CI、Anthropic 端点、代理槽位、多 API Key 等全部附属能力；
- 纯 Python 标准库（Python >= 3.9），零第三方依赖，`accounts/` + `usage/` 两个数据目录；
- 出口形态固定为：**国区、海外区各自同时只启用一个账号**，不做多号池的协调调度。

## 快速开始

```bash
cd ~/Projects/Utility/wb2api

# 1. 加账号（任选其一；token 也可带 --refresh 一起导入）
python3 -m gateway.wbctl login --realm intl          # 浏览器 OAuth
python3 -m gateway.wbctl import /path/to/auth.json   # 桌面端/旧反代的 JSON（自动识别）
python3 -m gateway.wbctl import '<accessToken JWT>'  # 或直接喂 token
python3 -m gateway.wbctl import - --refresh <rt>     # 或 stdin

# 2. 起网关
python3 -m gateway.server                            # 127.0.0.1:8788
python3 -m gateway.server --api-key sk-local         # 顺手持久化 key
python3 -m gateway.server --tasks                    # 打开后台签到/保活调度（默认关）

# 3. 用
export OPENAI_API_BASE=http://127.0.0.1:8788/v1
export OPENAI_API_KEY=***
```

## 支持的格式

OpenAI 兼容，路由面就这些（命令行细节看 [USAGE.md](USAGE.md)）：

| 路由 | 鉴权 | 说明 |
|---|---|---|
| `GET /health` | 免 | 存活探针 + 池状态摘要 |
| `GET /realm` | 免 | 当前出口 |
| `POST /realm` | key | 切换出口 intl/cn（落盘持久化） |
| `GET /v1/models` | 免 | 模型发现：默认吐双出口全量 + `cn:`/`global:` 别名；带 `X-Realm` / `?realm=` 只列该出口 |
| `GET /credits` | 免 | 各账号积分/余额 |
| `POST /v1/chat/completions` | key | Chat Completions，stream / 非 stream |
| `POST /v1/responses` | key | Responses API（Codex / Claude Code） |

key 仅当 `settings.json` 配了 `api_key` 才强制；没配就全放行（默认只绑
127.0.0.1）。只认 `Authorization: Bearer` / `x-api-key` / `api-key` 头，不接受
`?key=`（会泄进浏览器历史与 Referer）。CORS 默认关闭，`--cors` 显式打开。

## 模型调度（出口选择）

优先级从高到低：

1. **模型名前缀**（只认冒号）：`cn:hy3` 强制国内版出口，`global:hy3` 强制国际版
   （`intl:` 同义）。上游只收到裸名，响应与 `usage.jsonl` 回显你请求的带前缀名。
   前缀与模型专属出口冲突会 400。
2. **`X-Realm: cn|intl` 头或 `?realm=`**：逐请求钉死。
3. **自动路由**：单出口模型自己找家；双出口模型跟随全局 realm。
4. **全局默认 realm**：`wbctl realm cn` 或 `POST /realm`，落盘持久化。

`/v1/models` 的 `exitRealm` 字段标出每个条目属于哪个出口。

## 启动参数

| 参数 | 作用 |
|---|---|
| `--host` / `--port` | 监听地址与端口（默认 `127.0.0.1:8788`，同 `HOST`/`PORT`） |
| `--api-key` | 持久化唯一一把 API Key；传空字符串则清除它（鉴权关掉） |
| `--realm intl\|cn` | 切换默认出口并落盘（等价 `wbctl realm`） |
| `--tasks` | 打开后台调度：token 保活、国内签到、猫猫旅行、夜猫子任务、国际版日活 |
| `--cors` | 给 API 路由加 `Access-Control-Allow-Origin: *` |
| `--hide` | 忽略 kill/pkill 类信号，改由 systemd 经 `SIGRTMIN+4` 停止 |
| `--accounts-dir` / `--usage-dir` | 凭据目录、用量目录 |
| `--system-prompt` / `--system-prompt-file` | passthrough 的占位文本，或 custom/append 的提示词 |

## 控制字段（`accounts/settings.json`）

带名字的开关用 `wbctl set-switch <名字> [值]` 读写；`upstream.*` / `schedule.*` /
`prompt.*` 这三类直接改 `settings.json`（`prompt` 也可用 `wbctl set-prompt`）。

| 字段 | 默认 | 作用 |
|---|---|---|
| `reserve-credits` | 0=关 | 余额 ≤ 阈值的账号停止派单 |
| `daily-token-limit` | 0=关 | 账号当日 token 用量达上限即停放至本地零点 |
| `auto-switch-product` | off | 429 时轮换出站身分（三条独立配额线） |
| `daily-chat-web` | on | 国际版日活是否走网页通道 |
| `local-web-tools` | off | 网关自己执行 `web_search`/`web_fetch` |
| `expiring-window` | 7 天 | 临期积分优先派单；0=纯轮转 |
| `credits-refresh` | 12 小时 | 后台积分刷新器的陈旧阈值 |
| `upstream.header/idle_timeout_seconds` | 120/300 | 上游首包超时与流空闲超时 |
| `schedule.{checkin,travel,keepalive,cat}` | 9/21 · 9/21 · 22 · 1 | 后台排程时段，热加载免重启 |
| `prompt.mode` / `prompt.file` | passthrough | 系统提示词模式（passthrough/custom/append） |

`WB_CA_BUNDLE=/path/to/ca.pem`（优先）或 `SSL_CERT_FILE` 追加私有根证书，用于公司
代理 / TLS 解密后面的自签链；证书校验失败不重试，直接报带指引的 502。
`WB_PRESERVE_REQUEST_CONTRACT=1` 保留客户端数据原样（tool arguments 与 tool 结果
不消毒、namespace 子工具的 `strict`、Responses `text.format` 映射为
`response_format`），默认关。

## 注意事项

- **账号级失败治理默认生效**：429 指数退避 10min→2h；连续硬失败熔断 30min→6h；
  402 余额不足停放至次日本地 04:00，余额恢复提前解冻。连击计数受
  `WB_FAIL_WINDOW`（默认 600 秒）时间窗约束，隔久了从第一档重算。
- **`enable` / `disable` 只管推理**：`--tasks` 的保活/签到/旅行对每个能跑通的账号
  无条件执行，与开关无关。一次性成长任务用 `wbctl growthplan <uid>` 手动跑。
- **反风控是硬约束**：账号间 3~10 秒随机间隔，排程时刻每日 ±15~45 分钟随机偏移，
  出站头与请求体指纹对齐官方客户端。多号同 IP、高频并发、批量任务都会提高封号
  概率；低频自用 + 默认关 `--tasks` 是最稳姿态。
- **请求体上限 16MB**（`WB_MAX_PAYLOAD_BYTES`），超限返回 413 并附 `gateway_hint`
  说明该改哪个环境变量。
- **`accounts/` 是明文 token**，等同账号钥匙；备份/同步前自己加密。
- 本工具不提供任何账号与额度，仅限个人合法自有账号私有环境使用。

## 目录

每个分区一份 README.md，同名顶层 `.py` 是该分区的对外入口。

```
gateway/
  http/ protocol/ account/ upstream/ config/ tasks/ cli/ common/ data/
accounts/   # 0700；token 明文 JSON 0600，勿同步
usage/      # usage.jsonl 用量流水
tests/      # python3 -m unittest discover -s tests
```
