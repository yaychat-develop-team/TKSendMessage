# TKSendMessage · TKChatWave

面向 Windows 的 TikTok 直播评论桌面工具。项目在本机管理登录账号，按用户选择向一个或多个直播间发送评论，并通过独立直播消息连接观察评论是否实际出现在直播间。

> 本项目仅用于你拥有或已获授权的账号、直播间和测试场景。请遵守 TikTok 服务条款、当地法律以及直播间规则，不得用于骚扰、刷屏、规避平台限制或其他滥用行为。

## 功能概览

- 通过 TikTok 官方网页完成登录，支持二维码及页面实际提供的其他登录方式。
- 本地保存并管理多个账号，可编辑备注、筛选、批量勾选和重新登录更新。
- 支持单个或多个目标直播间；多个用户名使用 `;` 或 `；` 分隔。
- 支持串行和并行房间模式，并可配置发送次数、间隔、观察时间及重试策略。
- 使用独立 WSS 连接完成房间状态、进房响应、心跳和评论展示观察。
- 将“接口已接受”“已观察到展示”“未观察到”“监听不可用”和“发送失败”分别记录。
- 保存按日期划分的脱敏日志和按房间、账号隔离的运行报告。
- 提供 Windows 单文件打包脚本；源码也可直接运行。

## 运行要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 64 位 |
| Python | 3.10 或更高版本；项目验证环境为 3.10.19 |
| 浏览器 | Chrome、Chromium，或项目目录下的 `browser/chromium/chrome.exe` |
| 网络 | 能正常访问 TikTok 登录页、直播页面和直播消息服务 |

发布版会将 Python、Qt 和 Chromium 打入单个 EXE；本仓库不提交 EXE、浏览器文件、账号数据或运行缓存。

## 快速开始

### 1. 获取源码

```powershell
git clone https://github.com/yaychat-develop-team/TKSendMessage.git
cd TKSendMessage
```

### 2. 创建环境并安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
```

### 3. 启动桌面程序

```powershell
.\.venv\Scripts\python.exe app.py
```

如果项目没有内置 Chromium，程序会尝试使用系统浏览器；也可以通过 `TIKTOK_BROWSER_EXECUTABLE` 指定浏览器可执行文件。

### 4. 完成首次使用

1. 点击“新增 / 更新账号”，在打开的 TikTok 官方页面完成登录。
2. 登录采集完成后，回到主界面勾选本轮要使用的账号。
3. 输入主播用户名，例如 `creator` 或 `@creator`；多个直播间可写为 `creator_a;creator_b`。
4. 输入消息并设置每账号每直播间的发送次数与间隔。
5. 点击“开始发送”，在账号表和运行日志中查看接口结果及展示观察结果。

## 如何理解结果

HTTP `200` 或业务 `status_code=0` 只表示评论接口接受了请求，不能单独证明其他观众已经看到评论。

| 状态 | 含义 |
| --- | --- |
| 已观察到消息 / `delivered` | 独立监听在观察窗口内收到符合当前匹配规则的评论事件。 |
| 已接受 / 未观察到 / `accepted_not_observed` | 接口接受请求，但监听窗口内没有观察到对应评论。 |
| 监听不可用 / `listener_unavailable` | 无法建立或维持观察连接，因此不能判断是否展示。 |
| 发送失败 / `send_failed` | 消息请求、登录状态、房间状态或必要准备流程失败。 |
| 登录失效 | 平台明确要求重新登录；账号记录保留，更新登录后可继续使用。 |

展示观察是辅助证据，不是平台送达承诺。需要严格验收时，应在另一个正常观众端人工核对发送者、正文和消息 ID。

## 多直播间与并发

- 默认串行：同一账号按填写顺序处理直播间，不同账号仍可并发。
- 并行模式：最多同时处理 3 个直播间，其余目标排队。
- 计划请求量 = 勾选账号数 × 去重直播间数 × 每账号每直播间发送次数。
- “停止”会阻止尚未开始的后续请求，但不能撤回已经提交的评论。

请从较低频率开始测试。并发账号、直播间和发送次数越多，网络连接、内存占用以及平台风控风险越高。

## 数据与隐私

源码模式默认在项目目录创建运行数据；发布版在 EXE 同级创建：

```text
runtime/
├─ accounts/tiktok_accounts.json
├─ desktop-settings.json
├─ profiles/
├─ reports/
└─ logs/YYYY-MM-DD.log
```

`runtime/` 可能包含 Cookie、用户代理、Ticket Guard 会话材料和其他登录信息：

- 不要提交、截图或发送给其他人。
- 不要把使用过的 `runtime/`、浏览器 profile 或日志打进发布包。
- 仓库已忽略常见账号库、报告、密钥、日志、构建缓存和诊断输出。
- 如果凭证曾进入公开 Git 历史，仅删除文件并不安全，应立即让相关会话失效并重新登录。

## Windows 打包

先准备一个干净的 Chromium 目录，目录根部应包含 `chrome.exe`，且不能含任何登录 profile：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build\build.ps1 `
  -ChromiumPath 'D:\BrowserVendor\chrome-win64' `
  -PythonExecutable '.\.venv\Scripts\python.exe'
```

构建结果位于 `dist/TKChatWave.exe`。发布前请使用一次性运行目录进行验证，不要直接分发本机使用过的 `runtime/`。

macOS 必须在 Mac 上构建，详见 [`build/build_macos.sh`](build/build_macos.sh) 和 [`build/macos_one_click.py`](build/macos_one_click.py)。Windows 上的 PyInstaller 不能生成可运行的 macOS 应用。

## 测试

```powershell
python -m compileall -q app.py desktop tiktok_live build tests
python -X faulthandler -m unittest discover -v
```

离线测试使用合成数据，不会登录真实账号或发送真实评论。真实平台状态可能变化，发布前仍需在获授权的账号和直播间中进行小规模交互验收。

## 项目结构

```text
TKSendMessage/
├─ app.py                    # 桌面程序入口
├─ desktop/                  # Qt 界面、设置和后台工作线程
├─ tiktok_live/              # 账号、请求签名、房间与消息流程
├─ listen_msg.py             # 直播消息观察入口
├─ send_live_message.py      # CLI / Python 兼容入口
├─ settings.py               # CLI 默认配置
├─ build/                    # Windows/macOS 构建与发布校验脚本
├─ tests/                    # 离线回归测试和合成夹具
└─ docs/                     # 架构、操作和排障文档
```

## 文档

- [桌面使用说明](docs/desktop-usage.md)
- [业务与脚本说明](docs/usage.md)
- [项目架构](docs/architecture.md)
- [直播消息链路与失败点](docs/live-message-failure-points.md)
- [匿名监听代理说明](docs/observer-socks5.md)

## 常见问题

### 接口成功但直播间看不到消息

接口接受和公开展示是两个阶段。请检查日志中的展示分类、直播间评论限制、账号状态、评论过滤、观察连接及网络延迟，避免连续快速重发。

### 登录后账号没有保存

等待 TikTok 页面完整加载并返回登录状态。程序只有在关键登录 Cookie 和会话材料完整时才会保存；取消、超时或不完整登录不会覆盖已有账号。

### 程序提示 `initial_fetch_unavailable`

表示评论提交前的直播数据初始化没有成功。请确认主播正在直播、账号登录有效、网络可访问直播服务，并在降低频率后重试。

### 可以把账号文件发给别人排查吗

不可以。账号文件包含完整登录资料。只分享脱敏后的错误分类、HTTP 状态、业务状态和必要日志片段。

## 免责声明

本项目按现状提供，不保证对平台后续接口、风控或页面变化持续兼容。使用者需要自行承担账号、内容、网络和合规风险，并确保所有操作均已获得授权。
