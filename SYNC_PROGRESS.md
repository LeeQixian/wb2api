# 与上游 workbuddy2api-hub 的同步基线

上游仓库：[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)
（本地备份 `/home/Arisha/Projects/Watch/workbuddy2api-hub/`）

| 项 | 值 |
|---|---|
| 同步到 | `477ba36` release: v1.7.32 |
| 分叉点 | `650b659` release: v1.5.4（首次瘦身移植的基线） |
| 已跟进区间 | v1.5.5 → v1.7.32 |

同步口径：只跟进已发布的 tag，tag 之后的主干不读。上游 bug 修复照搬；上游新增
功能加开关且默认关；与「本地独有改动」冲突的一律以本地为准。

## 直接废弃的功能（上游有，本项目永远不做）

- dashboard 看板全套：区块导航、时序图、明暗主题、i18n、积分明细弹窗。
- 面板 / hub 专属：积分获取历史、登录限流、updates 检查、连登管家。
- Docker / CI / checksums 发布、便携打包、hub 部署脚本与 aliyun updater。
- 桌面端凭据扫描与 `$wbEncrypted` 解密、桌面端身份对齐。
- Anthropic `/v1/messages` 原生端点。
- `wb_agents` 一键配置客户端（含其 0600 写盘约定）。
- models.dev 四级上下文探测（外部数据源，违背零依赖）。
- 多 API Key / 按 key 限模型、代理槽位、按日与按模型限额矩阵。
- 测试基建并行化与 `_dom_stub.js`。

## 因理想形态废弃的功能（与「每出口只启用一个账号」冲突）

- 池内调度：会话亲和页内轮转、暖号 MRU、剩余用量优先、出口级退避、账号间错峰。
- 账号间轮换协调：11140 不换号与限制持久化、整池暂停、出站身分白名单。
- 面板化定址路由（`/u/<ref>/v1/...`）。
- 免费额度耗尽回退积分、隐藏无倍率模型、日活模型冷却回退、网页通道失败重试。
- 403 降级重发（本项目契约是 403 直通）。

## 其他已评估、明确跳过

- SSE 分块读写与共享 SSLContext（单机无感的高并发优化）、usage 索引、CRLF 归一。
- `daily_chat_model` 设置项：本地日活模型固定 `hy3`，不跟上游配置。

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

## 重点关心（跟上游盯这些）

- 风控与失败治理：429 / 402 / 403 分类，退避与熔断阶梯，时间窗计数，本机断网不记账。
- 出站身份与指纹：身份头族、user-agent、紧凑 JSON 请求体、per-account 连接池。
- 凭据：token 刷新与 401 处理、HTML 401 不误判为失效、证书链与 `WB_CA_BUNDLE`。
- 协议兼容：tool schema 规整、tool 结果配对修复、namespace / custom 工具、Responses 映射。
- 模型目录：倍率与出口归属、别名。
