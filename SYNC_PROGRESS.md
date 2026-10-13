# 与上游 workbuddy2api-hub 的同步基线

## 当前基线

上游仓库：github.com/ardeyouxipianyi/workbuddy2api-hub
（本地备份 /home/Arisha/Projects/Watch/workbuddy2api-hub/）

同步到：`4c6fab5`  release: v1.7.2（tag v1.7.2 之后 4 个提交）
移植起点：`650b659`  release: v1.5.4（首次瘦身移植的基线）

已跟进区间：v1.5.5 -> v1.7.2。上游 bug 修复照搬；上游新功能加开关默认关；
与「本地独有改动」冲突的一律以本地为准。

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
