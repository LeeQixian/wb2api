# 与上游 workbuddy2api-hub 的同步基线

上游仓库：[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)
（本地备份 `~/Projects/Watch/workbuddy2api-hub/`）

| 项 | 值 |
|---|---|
| 同步到 | `6123c54` release: v2.0.3（自 v1.9.1 起为选择性同步，见下） |
| 分叉点 | `650b659` release: v1.5.4（首次瘦身移植的基线） |
| 已跟进区间 | v1.5.5 → v2.0.3 |

同步口径：只跟进已发布的 tag，tag 之后的主干不读。上游 bug 修复照搬；上游新增
功能加开关且默认关；与「本地独有改动」冲突的一律以本地为准。

上游在 v2.0.0 的发布说明里写了「项目停止维护（2026-11-06）」，但 v2.0.1-v2.0.3
仍在发布，tag 口径不变。

## v1.10.9 → v2.0.3 选择性同步（80+ commits 中取 6 个）

已取：

| commit | 内容 |
|---|---|
| `5546063` + `4358541` | `guard_empty_text_parts`：空 `text` 段（含被上游嗅探器当 parts 解析的字符串数组）出站前填一个空格，避免 400/11151 把会话打退役；`mirror_upstream_reasoning`：kimi/glm/minimax/mimo/step/space-bunny 的 `reasoning_content` 镜像进 `reasoning`，边界为精确名或 `前缀-` |
| `d319bd0` | Responses 历史推理保真：连续 reasoning 项合并、reasoning 归属下一条 assistant 而非上一条、Chat 形状助手历史的 `reasoning_content`/`tool_calls` 保留、web 工具轮次回传本轮推理与唯一 call id |
| `4df31cf` | Claude Code 自述首段整句中性化；删掉把 `official CLI tool` 写回去的反向注入规则，并补小写字面量命中 |
| `2acd180` | 国际版 `deepseek-v4.1-flash` 出站改道 `-sg`（修 6004）；逻辑名保留，`WB_INTL_MODEL_ALIAS=0` 关闭 |
| `80865aa` | 未处理的上游状态码抛出前归还账号并发名额，避免账号被永久标记「占满」 |

未取（同区间，按废弃清单或与本地契约冲突）：

- `f840546` 可选 Go/uTLS 传输桥：外部二进制，违背零依赖与纯标准库。
- v2.0.3 的设备 Token、桌面 chat 兼容头（本地 `WB_DESKTOP_CHAT_COMPAT` 已有）、
  最小输出预算与空输出重试、国内 Kimi 工具 schema `$ref/$defs` 内联：属新功能，
  本地暂无需求，先不引。
- `f1de859`（#830 订阅桥）、`def6539`/`7bc095d`（一键启动工作台）、`47c6728`
  （`-m wb_proxy` 入口）、`d2e26fa`（#841 号池分区）：面板 / 部署 / 多号族。
- dashboard 布局修复（`8af8fc2`、`d957885`、`f028ecc`、`118395c`）、面板分页与
  登录槽、settings 分组 i18n、账号筛选重置。
- `7fd245f` + `826c8ca`（usage checkpoint 锁与补写）：本地 usage 是单次 `os.write`
  原子追加，无聚合缓存。
- `537cfde` / `5fcab44` / `edf95cc`（chat head 与 effort 日志）、`def6b7c`（Clash
  fake-IP 探针放行）、`95a7a03`（日活总开关，本地已有 `daily-chat-web`）。
- `91d509d` / `c844a8c` CodeArts 相关与 `bf2eadc` 账号筛选：出口/面板族。

## v1.9.1 → v1.10.9 选择性同步（145 commits 中取 9 个）

已取：

| commit | 内容 |
|---|---|
| `b04868e` | `normalize_tools` 丢弃上游不接受的名字（400/11133） |
| `ef479f7` | 嵌套 namespace 保留父路径；`resolve_namespaced_name` 用 `rfind`；Responses `tool_choice` 回写裸名 |
| `ecb95a6`（仅 sanitize 部分） | Codex 自述首段整句中性化，删掉反向注入指纹的旧改写规则 |
| `0ed5386` | 429 附 `X-RateLimit-Reset` / `-At` 头与正文 `reset_at`；废弃值回落本地窗口 |
| `97df705` | `WB_CACHE_HTTP_ERROR_BODY`：错误正文首次读取后复用，默认关 |
| `1bf70e5` | `WB_STOP_AT_SSE_DONE`：读到 `[DONE]` 即停，默认关 |
| `53c8697` | `usage.jsonl` 改单次 `os.write` 原子追加（本地无 msvcrt 分支，仅 POSIX） |
| `b700692` | 日限折叠 offset 加 size/dev/inode + 64 字节尾签名校验 |
| `42dbc90` | 国际版 `deepseek-v4.1-flash` 计费 `x0.00` → `x0.11` |

未取（同区间，按废弃清单或与本地契约冲突）：

- `ecb95a6` 的 proxy_slot / proxy_exit 归因：依赖代理槽基建，本地没有。
- `0ed5386` 的整池最短窗口口径：本地每出口单号，`retry_after_seconds` 已是同一窗口。
- `25b60cc` 请求体上限面板可调与 413 断连：依赖 `wb_settings` 面板接口，本地
  `WB_MAX_PAYLOAD_BYTES` 已覆盖。
- `39d6a76` 国内版 flash 免费误标：本地国内目录已是 `x0.11`，且无免费回退族。
- `dfe378d` 国际版计费快照大改（3800 行）：随下次目录全量同步再对，本次只改 flash。
- `937c179` 成长中心任务扩充、`769b74e`/媒体系列（`#728`）、CodeArts 第三出口全套、
  dashboard/登录槽/代理槽、`47c6728` OpenWrt 入口、`7fe04ad` 诊断脚本。
- `4df31cf`（Claude Code intro 消毒）在 v1.10.9 之后的主干，按口径未读。

## 直接废弃的功能（上游有，本项目永远不做）

- dashboard 看板全套：区块导航、时序图、明暗主题、i18n、积分明细弹窗。
- 面板 / hub 专属：积分获取历史、登录限流、updates 检查、连登管家。
- Docker / CI / checksums 发布、便携打包、hub 部署脚本与 aliyun updater。
- 桌面端凭据扫描与 `$wbEncrypted` 解密、桌面端身份对齐。
- Anthropic `/v1/messages` 原生端点。
- `wb_agents` 一键配置客户端（含其 0600 写盘约定）。
- models.dev 四级上下文探测（外部数据源，违背零依赖）。
- 多 API Key / 按 key 限模型、代理槽位、按日与按模型限额矩阵。
- 模型额度窗口探针 `wb_window_probe`（多号池刷额度专用：到点主动发探针把新一轮
  窗口起点钉在重置时刻）；本地单号自用，只保留重置时刻的持久化与惰性过期。
  上游 v1.9.0 给窗口加的 `cap` / `cap_at` 字段只取数据形状，落在凭证档
  `modelWindows` 行上，不引入探针与 `model_windows.json`。
- 测试基建并行化与 `_dom_stub.js`。

## 因理想形态废弃的功能（与「每出口只启用一个账号」冲突）

- 池内调度：会话亲和页内轮转、暖号 MRU、剩余用量优先、出口级退避、账号间错峰。
- 账号间轮换协调：11140 不换号与整池暂停、出站身分白名单。单个账号的限制窗口
  落盘（`restrictedUntil`）不在此列，见下。
- 面板化定址路由（`/u/<ref>/v1/...`）。
- 免费额度耗尽回退积分、隐藏无倍率模型、日活模型冷却回退。
- 日活网页通道的多模型候选序列 / 多轮回退重试（本地固定 `hy3`、单轮）；保留会话
  收尾治理（登记表 + 调度器回收）。
- 403 降级重发（本项目契约是 403 直通）。

## 其他已评估、明确跳过

- SSE 分块读写与共享 SSLContext（单机无感的高并发优化）、usage 索引、CRLF 归一。
- `daily_chat_model` 设置项：本地日活模型固定 `hy3`，不跟上游配置。
- v1.9.0 的 reserve 豁免零费率模型（`e38fcb2`）：属「免费额度耗尽回退积分」废弃族，
  且本地 `reserve_blocked()` 无 model 形参、账号上无 `free_models` 判定。
- v1.9.0 的 `allowed_accounts` / `key_readiness`（多 API Key 绑定）与 dashboard 三项
  布局修复（`a3f584a`、`ee4fa4d`、`fb1eb82`）。
- 池空时「候选只在等模型窗口 → 429」那支（`7b453f3`）：本地无 API Key 绑定，候选
  恒等于全区域可用号，`realm_model_throttled` 已先命中同一条件，照搬只会新增死代码；
  只取其中的报错文案（429 说清模型名与剩余秒数、503 去掉误述的日限额）。

## 本地独有改动（同步时永远保留，冲突以本地为准）

- wbctl 全套 CLI（替代面板），含昵称 / uid 前缀解析。
- `wbctl import` 三格式：JWT、桌面嵌套 `.info`、hub 扁平 JSON。
- `wbctl growthplan` 独立命令：一次性成长任务不走调度器。
- server 路由面固定：`/health`、`/realm`、`/v1/models`、`/credits`、`/v1/chat/completions`、`/v1/completions`、`/v1/responses`、`/v1/responses/compact`；移除 `?key=`、CORS 默认关、默认绑 127.0.0.1。
- `cn:` / `global:` 模型名前缀 + 双出口全量 `/v1/models`。
- realm 状态文件路径按 `--accounts-dir` 动态解析。
- accounts 热重载保留活对象（防 refresh 竞争覆盖 token）。
- translate / upstream 无全局状态，POOL、log、目录全部参数化。
- 调度开关（settings.json，默认关）：`account_max_concurrent`、`affinity_stable_key_enabled`。
- 模型别名 `model_aliases`：对象或列表两种写法，在协议转换与选号之前解析。
- 401 判死同时认 `WWW-Authenticate` 的 `invalid_token`（HTML 正文不再豁免真失效），
  派单与刷新两条路都携带挑战头；无挑战头或 `Basic realm=` 的代理页仍豁免。
- 上游 11140 限制窗口落凭证档 `restrictedUntil`：派单与日活都遵守，重启 / 刷新
  token / 换身分不清除，只有成功请求解除；不引入上游的多号连坐口径。
- 429 模型窗口落盘在账号凭证档（`modelWindows`），加载时挂回未到期项，到点靠
  时钟比较惰性放行；成功请求即时清除。不引入上游的 `model_windows.json`。
- 日活网页会话登记表：进程内有界（128 条），收尾失败保留待重试，调度器每 60 秒
  代为关闭卡死线程留下的残留会话。

## 重点关心（跟上游盯这些）

- 风控与失败治理：429 / 402 / 403 分类，退避与熔断阶梯，时间窗计数，本机断网不记账。
- 出站身份与指纹：身份头族、user-agent、紧凑 JSON 请求体、per-account 连接池。
- 凭据：token 刷新与 401 处理、HTML 401 不误判为失效、证书链与 `WB_CA_BUNDLE`。
- 协议兼容：tool schema 规整、tool 结果配对修复、namespace / custom 工具、Responses 映射。
- 模型目录：倍率与出口归属、别名。
