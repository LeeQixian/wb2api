# wb2api 使用手册

网关把 WorkBuddy 账号包装成 OpenAI 兼容接口。两个命令：

- `python3 -m gateway.wbctl ...`：加账号、查余额、改设置。
- `python3 -m gateway.server ...`：启动网关，前台运行，Ctrl-C 停止。

命令都在项目目录下执行；系统自带 `python3` 即可。

## 第一次使用

```bash
cd ~/Projects/Utility/wb2api

# 1. 加账号：打印一个网址，浏览器登录后命令自动完成（最长等 10 分钟）
python3 -m gateway.wbctl login --realm intl    # 国际版
python3 -m gateway.wbctl login --realm cn      # 国内版

# 2. 确认 ready=y
python3 -m gateway.wbctl list

# 3. 起网关
python3 -m gateway.server

# 4. 验证
curl -s http://127.0.0.1:8788/health
curl -s http://127.0.0.1:8788/v1/models | python3 -m json.tool
```

客户端 base URL 填 `http://127.0.0.1:8788/v1`。

## 账号管理

| 命令 | 说明 |
|---|---|
| `list` | 一行一个账号：uid、出口、昵称、开关、ready、token 剩余、余额、最近错误 |
| `login [--realm intl\|cn]` | 浏览器 OAuth 加账号；cn 账号自动完成首次签到 |
| `import <来源> [--realm] [--nickname] [--refresh]` | 导入凭据：裸 JWT、文件路径（桌面 `.info`、旧项目 JSON）、或 `-` 读 stdin |
| `remove <uid>` | 删除账号及其凭据文件 |
| `enable <uid>` / `disable <uid>` | 启用或停用；停用只影响派单，文件保留 |
| `refresh <uid>` | 立即刷新 token；平时无需手动执行 |
| `credits [uid]` | 余额明细，含每个积分包的剩余与到期时间 |
| `checkin <uid>` | 国内版手动签到 |
| `daily-chat <uid>` | 国际版每日活跃对话；网页通道跑完才算积分 |
| `growthplan <uid>` | 手动执行一次性成长任务（接受、点亮、领取） |
| `sync-nicknames` | 从网页控制台补回账号昵称 |

`list` 的输出示例：

```
7f3a9c21-4e5b-4d8a-9c11-000000000001  intl  小明  on  ready=y  exp=2 days  credits=512.3
```

`ready=n` 表示 token 失效，先试 `refresh <uid>`；仍失败则重新 `login` 或 `import`。

## 出口与身份

| 命令 | 说明 |
|---|---|
| `realm` | 查看当前默认出口 |
| `realm intl\|cn` | 切换默认出口并落盘；运行中的网关下次启动生效 |
| `product <uid> <workbuddy\|vscode\|cli>` | 切换出站身份（三条独立配额线），别名 `wb` / `desktop` / `vsc` / `ide` / `codebuddy` |

单次请求指定出口用模型名前缀 `cn:hy3` / `global:hy3`，或请求头 `X-Realm: cn`。

## 开关

`set-switch <名字> [值]`：不带值读取，带值写入；名字写错会列出全部合法值。

| 名字 | 默认 | 作用 |
|---|---|---|
| `reserve-credits <数字>` | 0 | 余额不高于该值的账号停止派单，防止用到零 |
| `daily-token-limit <数字>` | 0 | 单账号当日 token 上限，到顶停放到次日零点 |
| `auto-switch-product on\|off` | off | 429 时自动切换出站身份重试 |
| `daily-chat-web on\|off` | on | 国际版日活是否走网页通道 |
| `local-web-tools on\|off` | off | 网关自己执行搜索并回喂模型；会访问模型给出的网址 |
| `expiring-window <天数>` | 7 | 临期积分优先派单；0=纯轮转 |
| `credits-refresh <小时>` | 12 | 后台积分刷新间隔，仅 `--tasks` 时运行；0=关 |
| `schedule-hours` | 只读 | 查看排程时段 |

其余键直接编辑 `accounts/settings.json`：`upstream.header_timeout_seconds`、
`upstream.idle_timeout_seconds`、`schedule.*`、`prompt.mode`、`prompt.file`、
`model_aliases`、`account_max_concurrent`、`affinity_stable_key_enabled`。
除 `schedule` 外都需重启网关生效。

## 密钥与提示词

| 命令 | 说明 |
|---|---|
| `set-key [<key>]` | 设置访问密钥；不带参数则隐藏输入，`-` 读 stdin |
| `clear-key` | 删除密钥，本机请求全部放行 |
| `set-prompt [passthrough\|custom\|append] [--file 文件]` | 系统提示词模式；不带参数读取当前值 |

系统提示词模式：`passthrough` 原样使用客户端的 system 消息，缺失时补一句中性提示；
`custom` 用网关的提示词替换；`append` 在客户端消息后追加。

## 启动参数

| 参数 | 作用 |
|---|---|
| `--api-key sk-xxx` | 设置访问密钥（等价 `wbctl set-key`） |
| `--port 9000` | 更换端口 |
| `--host 0.0.0.0` | 监听所有网卡；跨机使用时务必配合 `--api-key` |
| `--realm cn` | 切换默认出口并落盘 |
| `--tasks` | 后台调度：签到、旅行、夜猫子、国际版日活、token 保活 |
| `--cors` | 允许浏览器跨域调用 |
| `--system-prompt` / `--system-prompt-file` | 指定提示词文本或文件 |
| `--hide` | 忽略终止信号，只认 `SIGRTMIN+4`；交给 systemd 管理时使用 |
| `--accounts-dir` / `--usage-dir` | 更换数据目录 |

后台常驻：

```bash
nohup python3 -m gateway.server >> gateway.log 2>&1 &
tail -f gateway.log
pkill -f "gateway.server"
```

## 直接调用接口

设了密钥就加 `-H 'Authorization: Bearer <key>'`。

```bash
# 对话
curl -s http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hy3","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -Ns http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hy3","stream":true,"messages":[{"role":"user","content":"讲个笑话"}]}'

# 指定国内版出口
curl -s http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"cn:hy3","messages":[{"role":"user","content":"你好"}]}'
```

免鉴权查询：`GET /health`、`GET /realm`、`GET /v1/models`、`GET /credits`。
