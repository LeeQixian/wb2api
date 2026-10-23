# wb2api 命令行使用手册

这个项目只有一个用途：把你的 WorkBuddy 账号变成 OpenAI 兼容接口。你在本机跑一个
网关进程，任何 OpenAI 客户端把地址指到 `http://127.0.0.1:8788/v1` 就能用。

全部操作都在命令行里完成，一共两个命令：

- `python3 -m gateway.wbctl ...` —— 日常管理：加账号、查余额、开关设置。
- `python3 -m gateway.server ...` —— 启动网关（前台运行，Ctrl-C 即停）。

以下命令都在项目目录下执行：

```bash
cd ~/Projects/Utility/wb2api
```

系统自带 `python3` 即可，无需安装任何东西。

---

## 一、第一次用：四步跑起来

```bash
# 第 1 步：加账号。会打印一个网址，用浏览器打开、登录，命令自己等着（最长 10 分钟）。
python3 -m gateway.wbctl login --realm intl          # 国际版账号
python3 -m gateway.wbctl login --realm cn            # 国内版账号

# 第 2 步：确认账号就绪（ready=y 才能用）
python3 -m gateway.wbctl list

# 第 3 步：起网关。保持这个终端开着，这就是服务器本体。
python3 -m gateway.server

# 第 4 步：另开一个终端，验证活着
curl -s http://127.0.0.1:8788/health
# 看到 "ok": true 就成了。模型列表：
curl -s http://127.0.0.1:8788/v1/models | python3 -m json.tool
```

然后把你的工具（任何 OpenAI 客户端）的 base URL 填 `http://127.0.0.1:8788/v1`。

---

## 二、wbctl 全部命令，一个个说清楚

### 账号管理

**`list`** —— 看所有账号的状态。日常用得最多的一条。

```bash
python3 -m gateway.wbctl list
```

输出长这样，一行一个账号：

```
7f3a9c21-4e5b-4d8a-9c11-000000000001  intl  小明   on   ready=y   exp=2 days      credits=512.3
```

| 字段 | 意思 |
|---|---|
| 第一列 | uid，账号编号，其他命令都靠它指定账号 |
| intl/cn | 这个账号属于哪个出口 |
| 昵称 | 账号名（没有就显示 uid 前缀） |
| on/off | 启用状态；off 的账号不参与派单 |
| ready | y=能用；n=token 过期或坏了，见 `refresh` |
| exp | token 还剩多久（网关会自动续期，一般不用管） |
| credits | 余额（没查过的账号这里显示 `-`） |
| err: xxx | 最近一次上游错误；没有就不显示 |

**`login [--realm intl|cn]`** —— 浏览器登录加账号。跑完把 URL 贴到浏览器，登录成功
命令自动保存并退出。cn 账号会自动完成首次签到。

**`import <来源> [--realm intl|cn] [--nickname 名字] [--refresh 续期token]`** ——
不想开浏览器时的替代：直接把已有凭据喂进来。来源三选一：

```bash
# ① 一个裸 token 字符串
python3 -m gateway.wbctl import 'eyJhbGci....(很长的JWT)'

# ② 一个凭据文件的路径（桌面端的 .info、旧项目的 accounts/*.json 都能自动识别）
python3 -m gateway.wbctl import ~/Downloads/workbuddy-desktop.info

# ③ 从键盘/管道输入
python3 -m gateway.wbctl import -
```

realm 一般不用指定：token 里自带签名信息，程序能认出来是国内版还是国际版。

**`remove <uid>`** —— 删掉一个账号（连凭据文件一起删）。

**`enable <uid>` / `disable <uid>`** —— 临时停用/恢复。文件还在，只是不参与派单。
某个号快见底了就 disable，省着点用它。

**`refresh <uid>`** —— 立刻强制刷新 token。正常情况下不用管：派单时 token 快到期
（剩 2 分钟内）网关会先自动续期再发请求，`--tasks` 开着还有定时保活巡检。这条只在
`list` 里看到 `ready=n` 时手动救急用。刷不动（refresh 也过期）只能重新
`login`/`import`。

### 查余额和薅积分

**`credits [uid]`** —— 查余额明细，带每个积分包的剩余和到期时间。省略 uid 查全部账号。

```bash
python3 -m gateway.wbctl credits
```

**`checkin <uid>`** —— 国内版账号手动签到。开了 `--tasks` 会自动签；没开的话想起来
就跑一下，一次一条命令的事。

**`daily-chat <uid>`** —— 国际版账号的每日活跃对话（官方给 30/50 积分的羊毛）。
默认会多走一次网页通道会话——实测只有跑完的网页 agent 会话才算数，纯 API 对话不计。

**`growthplan <uid>`** —— 手动跑一遍一次性成长任务（接受 / 点亮 / 领取），跑完打印
earned 和逐条日志。这些任务**不在** `--tasks` 的自动调度里，只能这样手动触发；
企业账号没有成长系统，会被跳过。

**`sync-nicknames`** —— 导入的账号没名字？这条从网页控制台把昵称拉回来，
不用重新登录。只取昵称，别的个人信息一概不要。

### 出口（国内版/国际版）

**`realm [intl|cn]`** —— 不带参数=看当前全局出口；带参数=切换并落盘（重启保持）。

```bash
python3 -m gateway.wbctl realm         # 输出当前值，比如 intl
python3 -m gateway.wbctl realm cn      # 切到国内版
```

切了之后正在运行的网关不受影响（它是启动时读的）；下次启动生效。要对单次请求指定
出口，用模型名前缀 `cn:hy3` / `global:hy3`，或者请求头 `X-Realm: cn`。

### 出站身份

**`product <uid> <workbuddy|vscode|cli>`** —— 一个账号可以伪装成三种官方客户端发请求，
被限流时这是三条独立配额线。切了会落盘、重启保持，默认 workbuddy。
名字还接受别名 `wb` / `desktop` / `vsc` / `ide` / `codebuddy`。

### 开关（settings）

**`set-switch <名字> [值]`** —— 不带值=读，带值=写。所有开关默认都是关/保守的，
不碰它们网关行为和文档描述完全一致。逐个说：

```bash
python3 -m gateway.wbctl set-switch                       # 名字打错会列出全部合法值
```

| 名字 | 默认 | 作用 |
|---|---|---|
| `reserve-credits <数字>` | 0（关） | 余额 ≤ 该值的账号停止派单。防账号被用到零（到零上游会发催命短信） |
| `daily-token-limit <数字>` | 0（关） | 单账号当日 token 用量到上限就休息到第二天零点；全池都到上限时返回 429 |
| `auto-switch-product on/off` | off | 被 429 限流时自动换出站身份重试（配合上面 product 那三条配额线） |
| `daily-chat-web on/off` | on | 国际版日活是否走网页通道（走=能拿到积分，关=纯 API 对话不计分） |
| `local-web-tools on/off` | off | 模型说"我要搜索"时由你的网关自己去搜（DuckDuckGo，无需 key）并回喂模型。开了意味着网关会访问模型给的任意网址，自用再开 |
| `expiring-window <天数>` | 7 | 积分快过期的账号优先派单，先把要作废的花掉。0=关闭，纯轮流 |
| `credits-refresh <小时>` | 12 | 后台积分刷新频率（只在 `--tasks` 时运行）。0=关 |
| `schedule-hours` | 只读 | 看当前排程时段（默认签到旅行 9/21 · 保活 22 · 夜猫 1）。改时段要编辑 `settings.json` 的 `schedule` 段，调度器每轮热加载、不用重启 |

这几个之外的 `settings.json` 键没有 `set-switch` 名字：`upstream.header_timeout_seconds` /
`upstream.idle_timeout_seconds`（首包与流空闲超时）、`schedule.*`、`prompt.mode` /
`prompt.file`（后者用 `wbctl set-prompt`）、`model_aliases`（入口名映射，对象或
`[{"from","to"}]` 两种写法）、`account_max_concurrent`（单账号在飞请求上限，0=不限）、
`affinity_stable_key_enabled`（会话键额外覆盖首条 assistant 回复）。直接编辑
`accounts/settings.json`，除 `schedule` 外都要重启网关才生效。

`--tasks` 的 token 保活默认提前 48 小时续期（`WB_KEEPALIVE_LEAD_HOURS` 可调，必须
大于巡检间隔）；巡检只在各任务自己的配置小时执行，`wbctl` 的手动触发会跑完整一轮。

### 密钥

**`set-key [<key>]`** —— 给网关设访问密码。不带参数会隐藏输入让你敲；`-` 从 stdin 读。
设了之后，所有推理请求必须带 `Authorization: Bearer <你的key>`，否则 401。
只绑 127.0.0.1 的本机模式下可设可不设；设了防的是本机其他用户/误暴露。

**`clear-key`** —— 删掉密码，本机请求全放行。忘了 key 就 clear 了再 set。

### 系统提示词

**`set-prompt [passthrough|custom|append] [--file 文件]`** —— 不带参数=读当前值。

背景：网关往上游发请求时必须带一条 system 消息（上游协议要求）。三种模式：

- `passthrough`（默认，什么都不用管）：客户端带了 system 就原样用；没带就补一句
  中性的 `You are a helpful assistant.`
- `custom`：客户端带的 system 全删掉，换成网关自己的提示词（`--file` 指定文本文件，
  不指定就用内置的一段中文工程助手提示词）
- `append`：客户端 system 保留，在其后追加网关的提示词

---

## 三、server 启动参数

`python3 -m gateway.server` 裸跑就是最安全的默认：只绑 127.0.0.1、端口 8788、
无鉴权、后台羊毛全关。需要的改动基本就这几个：

| 参数 | 干什么 | 什么时候用 |
|---|---|---|
| `--api-key sk-xxx` | 顺手把访问密码设了（等价 `wbctl set-key`；传空字符串则清除它） | 想让请求带密码 |
| `--port 9000` | 换端口 | 8788 被占 |
| `--realm cn` | 切换默认出口并落盘（等价 `wbctl realm cn`） | 想固定走国内版 |
| `--tasks` | 打开后台自动调度：国内版签到/猫猫旅行/夜猫子任务 + 国际版日活 + token 保活 | 想全自动薅积分。默认关，关着=纯网关。一次性成长任务不在这里，用 `wbctl growthplan <uid>` 手动跑 |
| `--cors` | 允许浏览器网页直接跨域调本地网关 | 只有网页版客户端直连本机时才需要 |
| `--host 0.0.0.0` | 监听所有网卡 | 局域网给别的设备用——先想清楚，必须配合 --api-key |
| `--system-prompt` / `--system-prompt-file` | 指定 passthrough 的占位文本，或 custom/append 用的提示词文本 | 配合 `set-prompt custom/append` |
| `--hide` | 忽略 `SIGTERM/SIGINT` 等，只认 `SIGRTMIN+4` | 交给 systemd 管（见本机 LOCAL.md）；前台调试别加 |
| `--accounts-dir` / `--usage-dir` | 换数据目录 | 一般不用 |

想让网关在后台常驻（关掉终端也不死）：

```bash
nohup python3 -m gateway.server >> gateway.log 2>&1 &
tail -f gateway.log          # 看日志
pkill -f "gateway.server"    # 停掉
```

---

## 四、直接手动调接口（curl 三板斧）

设过 key 就把 `-H 'Authorization: Bearer <key>'` 加上，没设就不用。

```bash
# 1. 对话（最常用）
curl -s http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <key>' \
  -d '{"model":"hy3","messages":[{"role":"user","content":"你好"}]}'

# 2. 流式
curl -Ns http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hy3","stream":true,"messages":[{"role":"user","content":"讲个笑话"}]}'

# 3. 指定走国内版出口（两种方式任选）
curl -s http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"cn:hy3","messages":[{"role":"user","content":"你好"}]}'
```

免鉴权的查询口：`GET /health`（状态）、`GET /realm`（当前出口）、
`GET /v1/models`（模型列表，加头 `-H 'X-Realm: cn'` 只看单出口）、`GET /credits`（余额）。

接自己的工具时，工具的 base URL 一律填 `http://127.0.0.1:8788/v1`（注意带 `/v1`），
API key 填你 set-key 的值。
