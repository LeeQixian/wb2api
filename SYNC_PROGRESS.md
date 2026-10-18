# 与上游 workbuddy2api-hub 的同步基线

## 当前基线

上游仓库：github.com/ardeyouxipianyi/workbuddy2api-hub
（本地备份 /home/Arisha/Projects/Watch/workbuddy2api-hub/）

同步到：`d39edf1`  release: v1.7.13
移植起点：`650b659`  release: v1.5.4（首次瘦身移植的基线）

已跟进区间：v1.5.5 -> v1.7.13。上游 bug 修复照搬；上游新功能加开关默认关；
与「本地独有改动」冲突的一律以本地为准。只跟进已发布 tag，tag 之后的主干不读。

v1.7.4 -> v1.7.13 移植明细（c7dad8f..d39edf1）：
- 49f19e5/4ed27f1 出站身份头族收敛到单一数据源，补 CLI 会话头、按重试隔离会话
- 工具 schema 规范化：parameters 补齐 type/object，functions 归一为 tools，空列表丢弃
- 166a02b 熔断按时间窗计次（WB_FAIL_WINDOW，默认 600s），本机断网不计入账号
- 80a4cfe 两条软限流阶梯共用同一时间窗；剩余时间按秒显示；通用失败记日志
- b843ff3 免费同胞过滤只认实时目录，收费模型不被静态快照中的旧免费项顶掉
- 414f204 glm-5.3-flash 强制纯文本（含 cn:/global: 别名），带 vision_note
- 50ddd72 413 响应带 gateway_hint；GPT 家族 max_tokens 低于 16 夹到 16
- d39edf1（仅非面板部分）请求结束按回包 credit 扣减缓存余额，余额守卫不再失效

不移植（逐项理由）：
- ed2522e/e7aa3ce 免费额度耗尽回退积分：本地不用积分回退
- 855336b 网页通道重试、60b7c8b 日活模型回退与 cached_tokens 对齐：前者是功能，
  后者与本地 DAILY_CHAT_MODEL 决定冲突且回包不写回上游
- 97925e9/a979b0a 隐藏无倍率模型：会把收费模型从目录里摘掉，本地要看真实倍率
- 2364a5e 账号间 2~5 秒错峰：本地调度器已是 3~10 秒
- 9d40841 一键配置 0600：本地没有 wb_agents 一键配置
- 面板、日志库、Anthropic 端点等见「明确不同步」

v1.7.3 移植明细（4c6fab5..c7dad8f）：
- de4fb53 webagent 补发 session/set_model（缺了它网页腿跑 Auto 扣积分）
- ddda600 daily_chat ok 以网页腿为准，失败不再谎报成功
- 17c0869 exc_detail：hiccup/失败日志带底层原因，不再只有一串 URLError
- 本地决定：DAILY_CHAT_MODEL 由 deepseek-v4.1-flash 改 hy3（均 x0.00，
  不跟上游的 daily_chat_model 设置项，不动 ds 额度）
- 不移植：bce5c96/3c80073 SSE 分块读写与共享 SSLContext（高并发优化，单机无感）、
  761db37 daily_chat_model 设置、195753c usage 索引（看板专属）、89802ad CRLF

## 明确不同步

wb2api 是单人本地瘦身网关，下列上游能力依赖的东西本项目没有：

- dashboard 看板（语言切换、区块导航、时序图、明暗主题、积分明细弹窗等全部）
- 多 API Key / 按 key 限制模型（本项目只有一把可选 key）
- 代理槽位 proxy slots（本项目只有 per-account proxy URL）
- Docker / CI / checksums 发布
- 桌面凭据扫描与 $wbEncrypted 解密（本机没有桌面端）
- 按日积分限额、按模型 token 限额、realm 矩阵化限额
- Anthropic /v1/messages 原生端点
- models.dev 四级上下文探测（外部数据源，违背零依赖）
- 测试基建并行化 / _dom_stub.js
- 403 降级重发（degrade.go；本项目契约是 403 直通）
- 池内调度：会话亲和页内轮转 / 暖号 MRU / 剩余用量优先调度 / 出口级退避
  （每个时刻只启用一个账号，不需要账号间协调）
- 面板专属的积分获取历史、登录限流、updates 检查、连登管家
- 网页通道失败重试、日活模型冷却回退（本地日活模型固定为 hy3）
- 隐藏上游未配置倍率的模型（本地要看真实倍率，不摘目录）
- 一键配置客户端（wb_agents 及其 0600 写盘约定，本项目没有该功能）

## 本地独有改动

同步时永远保留，冲突以本地为准：

- wbctl 全套 CLI（替代面板），含昵称/uid 前缀解析
- wbctl import 三格式（JWT / 桌面嵌套 .info / hub 扁平 JSON）
- wbctl growthplan 独立命令（一次性成长任务不走调度器）
- server 仅 7 条路由、?key= 移除、CORS 默认关、127.0.0.1 默认绑定
- cn:/global: 模型名前缀 + 双出口全量 /v1/models
- realm 状态文件路径按 --accounts-dir 动态解析
- accounts 热重载保留活对象（防 refresh 竞争覆盖 token）
- translate/upstream 无全局：POOL/log/目录全部参数化
- --tasks 的保活/签到/日常对所有能跑通的账号无条件执行，与 enable/disable 无关
