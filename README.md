# TKChatWave

Windows 桌面工具，复用独立复制的 `tiktok_live` 业务核心。主界面只需填写**目标直播间用户名**和**消息内容**，新增或更新账号时会打开独立 Chromium 窗口进入官方登录页面。支持二维码、手机号、邮箱、Google、Facebook 等官方页面实际提供的登录方式。

## 使用发布版

发送前会等待目标直播间入房响应、至少 30 秒及三次成功发出的心跳。
心跳发送间隔可在运行设置中调整，默认 10 秒（1–60 秒）；断线、房间不匹配或预热超时会阻止发送。
启用独立监听时，连接重试耗尽会终止本次任务；`s_v_web_id` 有值则带、无值则省略。
接口受理和独立监听接收不等于其他登录账号的网页已显示，仍需区分验证结果。

1. 将 `dist/TKChatWave.exe` 复制到可写目录，例如自己的用户目录。
2. 双击 `TKChatWave.exe`。发布版为单文件程序，只分发这一个 EXE 即可；首次运行会在 EXE 同级自动创建 `runtime/`。
3. 点击“新增 / 更新账号”完成登录。每次登录使用新的无痕 Chromium 会话；程序会在窗口关闭前提取并保存 Cookie。此按钮只保存账号，不发送消息。
4. 勾选本轮要发送的账号，填写目标主播用户名（可带 `@`）与消息，再点击“开始发送”。未勾选账号不会发送；没有勾选时发送按钮不可用。
5. 账号列表会显示登录页面识别到的用户名；旧记录显示“未获取用户名”时，使用“新增 / 更新账号”重新登录同一账号即可补全。
6. 选中账号行后可点击“查看详情”编辑并保存完整记录（含 Cookie 等敏感信息），或点击“删除账号”。“全选 / 清空选择”控制本轮发送账号。账号表有独立滚动条；拖动上方内容与日志之间的分隔条可调整高度。日志右下角的“清空控制台”只清除窗口显示，不删除每日文件。
7. 点击右上角“设置”，可调整登录超时、账号间隔、弹幕确认、重试次数与间隔、监听连接和 roomId 兜底 Cookie；保存后写入 `runtime/desktop-settings.json`。
8. 点击“停止”请求取消；当前请求或监听等待完成后，在下一账号或重试边界停止。关闭程序也会先等待后台工作安全退出。

匿名监听可在“设置 → 监听连接”中单独填写 SOCKS5 代理，留空沿用原有网络。只影响匿名初始化、WSS 和重连，不改变账号 fetch、发消息及心跳的网络。详见 [匿名监听代理说明](docs/observer-socks5.md)。

发布版包含 Python、Qt 依赖和固定版本 Chromium，无需安装 Python、Chrome、Node.js 或 Playwright。需要 Windows 10/11 64 位及能够访问 TikTok、登录服务与直播弹幕流的网络。尚未在另一台干净 Windows 机器上验收。

账号、Cookie、会话与桌面设置保存在 EXE 同级 `runtime/`，不会自动读取原项目账号。复制 EXE 不会带走已有 Cookie；`runtime` 含敏感登录资料，仅在本机妥善保管，不随发布包分发。完整操作、结果解释和排障见 [桌面使用说明](docs/desktop-usage.md)，CLI 兼容入口见 [脚本与业务说明](docs/usage.md)。

## macOS 构建

macOS `.app` 必须在 Mac 上构建，Windows 上的 PyInstaller 不能跨平台生成可运行的 macOS 程序。把源码复制到 Mac，准备与目标 Mac 架构匹配的 `Chromium.app`，然后执行：

最简单的方式是在 Mac 上安装 Python 3 以及 Google Chrome、Chrome for Testing 或 Chromium，解压源码包后双击 `Build-macOS.command`。脚本会创建独立构建环境、自动查找浏览器、安装项目依赖并生成 `dist-macos/TikTokLiveTool-mac.zip`。这个最终 ZIP 已包含 Python、Qt、业务依赖和浏览器，可以直接发给相同 CPU 架构的 Mac 用户；最终用户无需安装 Python 或浏览器。

如果 macOS 因下载文件权限而不允许双击，可在终端进入项目目录后执行一次 `chmod +x Build-macOS.command`，再双击运行。

也可以手动构建：

```bash
cd /path/to/tiktok_desktop_tool
chmod +x build/build_macos.sh
./build/build_macos.sh /Applications/Chromium.app
```

输出为 `dist-macos/TikTokLiveTool.app`，包含 Chromium。首次运行后，账号 Cookie、配置、报告和日志保存到 `~/Library/Application Support/TikTokLiveTool/runtime/`，不会修改 `.app` 包。未签名应用在其他 Mac 上可能需要完成开发者签名、公证或由用户在系统安全设置中允许打开。本项目不会自动提交源码或产物到 GitHub。
## 源码运行与构建

本仓库仅包含源码、测试和文档，不包含账号、Cookie、代理凭据、浏览器登录资料、构建缓存或 EXE。`runtime/`、`dist/`、`releases/` 和本机临时排查目录不得加入 Git；发布时仅分发经过验证的干净程序包。

开发环境使用 Python 3.10 或更高版本；已验证环境为 Python 3.10.19。桌面依赖使用 `PySide6-Essentials`，不使用 QtWebEngine。PowerShell 示例：

```powershell
cd D:\Myproject\forya\tiktok_desktop_tool
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe app.py
```

源码桌面模式优先使用 `browser/chromium/chrome.exe`；未放置浏览器时，DrissionPage 可使用系统 Chrome 或 `TIKTOK_BROWSER_EXECUTABLE` 指定的可执行文件。发布版必须包含内置 Chromium，优先级高于此环境变量。

构建需要一个只包含 Chromium 程序文件的干净目录（根目录有 `chrome.exe`），不能包含登录资料，也不能与项目的浏览器暂存、构建或发布目录重叠：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build\build.ps1 -ChromiumPath 'D:\BrowserVendor\chrome-win64' -PythonExecutable '.\.venv\Scripts\python.exe'
```

单文件构建使用 `build/tiktok_desktop_onefile.spec`，把 Python、Qt、业务源码和固定 Chromium 打入 `dist/TKChatWave.exe`。发布前需执行离线回归及冻结 GUI/内置浏览器冒烟；构建不会下载浏览器，也不应将带账号数据的已使用目录当作发布输入。

单独检查：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -X faulthandler -m unittest discover -v
python build/verify_release.py dist/TKChatWave
python build/smoke_release.py dist/TKChatWave
python -m compileall -q app.py desktop tiktok_live build tests
```

离线测试使用模拟依赖与合成数据。发布冒烟在一次性副本上启动冻结 GUI，并通过本地 CDP 打开内置 Chromium 的 `about:blank` 后退出；不完成真实登录或发送。真实账号保存、各登录方式/OAuth 窗口切换和一次真实发送需要用户交互验收，当前不声称已验证。

## 目录

```text
tiktok_desktop_tool/
├─ app.py                       # 桌面入口
├─ desktop/                     # Qt 界面、线程与运行路径
├─ tiktok_live/                 # 账号、签名、直播间、发送业务
├─ send_live_message.py          # 兼容 CLI / Python 导入入口
├─ listen_msg.py                 # 弹幕监听
├─ settings.py                  # CLI 输入与源码高级默认值
├─ requirements*.txt
├─ browser/chromium/            # 构建暂存的浏览器（忽略）
├─ runtime/                     # 桌面运行资料（忽略）
├─ build/                       # 构建、校验与冒烟脚本
├─ dist/TKChatWave.exe           # 单文件发布版（忽略）
├─ tests/
└─ docs/
```

本仓库与 `D:\Myproject\forya\tiktok` 独立，签名、账号去重和分类逻辑延续复制的业务实现。 登录时按账号保存浏览器实际 UA 和 Ticket Guard 原始状态。后续请求复用 UA，但每一次消息 POST（含重试）都用该账号状态与当前时间重新生成 Ticket Guard 签名，不复用缓存请求头；状态缺失或无效时停止发送并提示重新登录。`ROOM_ID_COOKIE` 保持为空；桌面用户无需填写 Cookie。完整 Cookie、`msToken`、CSRF、签名和 Ticket Guard 密钥不进入桌面日志或发布包；只有用户主动点击“查看详情”时才在本机详情窗口显示保存记录。输入的消息正文可能进入日志，请勿将凭据填入消息。
# Direct WSS integration

room_id 查询沿用原脚本的 curl_cffi 会话与直播页请求，先尝试账号 Cookie、再尝试已配置的备用 Cookie。
页面返回 WAF、临时 TLS/超时、HTTP 错误或没有 room_id 时，同一会话查询 `/api-live/user/room/`，用户名动态传入；接口临时网络故障/服务端故障最多尝试两次。
这些请求共用运行设置的查询超时预算，预留接口重试时间；只查询 room_id 不强制等待 SDK 令牌刷新。消息发送前仍单独刷新令牌，发送和心跳逻辑不变。
查询结果分别提供房间 ID 和开播状态：状态 4 保留查询到的 ID 但标记未开播，未知状态不视为在线；两者均禁止后续 WSS/发送。页面 ID 按目标主播的 hydration 状态绑定，不选推荐房间 ID。
查询不启动浏览器，不输出 Cookie 或令牌。

运行设置现按以下规则生效：账号仍然并发，“账号启动错峰间隔”仅控制同一直播间
各账号的启动间隔，0 秒同时启动（新设置默认 0，已有保存值保持不变）。
主界面发送次数为每账号每直播间的计划消息数；每条消息总尝试次数包含首次，
临时网络/HTTP 故障和未观察到消息共用这一上限。已观察成功、登录失效、禁言、
明确终止和取消不再重试。未观察到后的重发可能产生重复弹幕。
WSS 超时、失败后重试次数及重试间隔同时用于访客监听和账号初次连接/预热；
每次账号新连接仍需至少 30 秒、3 次成功心跳、18 秒跨度和服务端入房响应。
心跳发送间隔同时用于账号和访客连接，默认 10 秒、范围 1–60 秒。账号预热等待 3 个心跳周期，例如 5 秒间隔预热 15 秒、10 秒间隔预热 30 秒；同时必须成功发送至少 3 次目标房间心跳并收到进房响应。每次实际发送心跳都会打印角色、房间和序号；确认收到对应弹幕后，“消息成功展示”以绿色显示。
已建立账号连接中途断开则停止后续发送，不自动重放已发送的消息。
“显示弹幕”进入界面日志，保留房间/账号上下文和敏感内容过滤。
界面与保存文件的数值范围一致（秒数上限 3600）；非法或非有限数值回退默认值。

The desktop listener uses the supplied standalone script's Python-only
`standalone_audience_direct_v1` profile. Visitor Cookie initialization observes
delivery; separate account Cookie connections supply room heartbeats. Each
account waits at least 30 seconds, three successful target-room heartbeats and
a server entry response before sending. Connections stay open during the burst.
TikTokLive and Node are not runtime dependencies. Offline tests do not establish
real-platform connectivity or message visibility; no live account is used by
the offline release checks.

