# TikTok 直播消息桌面工具设计

日期：2026-10-14

## 目标

在 Windows 上提供可直接分发的桌面工具。用户只需填写目标直播间用户名和消息内容，即可使用已保存账号执行现有的直播消息发送流程。新增账号时，DrissionPage 启动的 Chromium 必须显示在工具内部。

首版以稳定复用现有 Python 业务代码为优先，不重写签名、Cookie、Ticket Guard、直播间识别、弹幕监听和结果统计逻辑。

## 项目隔离

新项目位于 `D:\Myproject\forya\tiktok_desktop_tool`。

从 `D:\Myproject\forya\tiktok` 复制源码、测试和文档到新目录，然后只在新目录中开发桌面功能。复制时排除：

- `tiktok_accounts.json` 以及任何真实账号 Cookie；
- `.browser-profile`、`port` 和浏览器用户数据；
- `__pycache__`、构建缓存和临时文件；
- `failed_accounts.json`、`silenced_accounts.json` 和旧日志。

原项目保持不变，可继续独立运行。

## 技术方案

- GUI：PySide6。
- 浏览器自动化：保留 DrissionPage 4.1.1.2。
- 浏览器嵌入：使用 `browser.process_id` 定位 Chromium 进程，通过 Windows API 枚举对应顶层窗口，使用 `SetParent` 嵌入 Qt 容器，并在容器缩放时调用 `MoveWindow`。
- 后台执行：登录、发送和监听运行在 Qt 工作线程中，禁止在 GUI 主线程执行阻塞网络操作。
- 打包：PyInstaller `onedir`。
- 浏览器：发布目录携带固定版本 Chromium；程序优先使用随包浏览器，不依赖用户电脑安装 Chrome。

## 目录结构

```text
tiktok_desktop_tool/
├─ app.py                         # 桌面入口
├─ settings.py                    # 业务默认配置
├─ send_live_message.py           # 兼容命令行入口
├─ listen_msg.py
├─ tiktok_live/                   # 复制并隔离的现有业务核心
├─ desktop/
│  ├─ __init__.py
│  ├─ main_window.py              # 主窗口和界面状态
│  ├─ workers.py                  # 登录与发送工作线程
│  ├─ browser_embed.py            # Chromium 窗口查找、嵌入、缩放与恢复
│  ├─ log_bridge.py               # Loguru 到 Qt 信号
│  └─ account_view.py             # 脱敏账号列表模型
├─ runtime/
│  ├─ accounts/                   # 新项目自己的账号库
│  ├─ profiles/                   # Chromium 登录资料
│  ├─ reports/                    # 失败、禁言结果
│  └─ logs/
├─ browser/chromium/              # 发布版内置浏览器
├─ build/
│  ├─ tiktok_desktop.spec
│  └─ build.ps1
├─ tests/
└─ docs/
```

## 首版界面

主窗口分为四部分：

1. 必填参数区：目标直播间用户名、消息内容。
2. 账号区：脱敏显示账号用户名、短账号 ID 和状态；提供“新增/更新账号”按钮。
3. 浏览器区：默认显示说明；登录时显示嵌入 Chromium；登录完成后自动收起。
4. 运行区：开始、停止按钮，当前阶段、进度统计、失败账号和实时日志。

线程数、监听超时、发送总次数、间隔等参数继续读取 `settings.py`。首版不要求用户填写，也不在主界面占用空间。

## 登录流程

1. 用户点击“新增/更新账号”。
2. 登录工作线程以随机调试端口和独立用户目录启动随包 Chromium。
3. 工作线程发出浏览器 PID；主线程定位窗口并嵌入浏览器区。
4. 用户可以扫码、手机号、Google 或 Facebook 登录。
5. 只有检测到完整关键 Cookie 和 `s_v_web_id`，并完成 TikTok 页面跳转后，才保存账号。
6. 按 UID 去重；重复账号覆盖 Cookie，并在界面显示对应账号 ID。
7. 捕获 Ticket Guard 状态后关闭浏览器，刷新账号列表并收起浏览器区。
8. 用户关闭浏览器、登录超时或嵌入失败时，释放窗口句柄和进程，并显示可理解的错误信息。

OAuth 新开窗口属于同一 Chromium 进程树。窗口监控器发现新顶层窗口后，把当前活动窗口切换到嵌入容器，避免登录窗口出现在工具外部。

## 发送流程

1. 校验直播间用户名和消息不为空。
2. 若没有账号，提示并自动进入新增账号流程。
3. 后台线程调用复制项目内的 `run_saved_accounts()`。
4. 实时日志通过 Qt 信号传入界面，不阻塞业务线程。
5. 每个账号的最终分类更新到统计区域；完整 Cookie、`msToken`、CSRF、Ticket Guard 私钥不进入界面日志。
6. 点击停止时设置取消标记；当前网络请求完成后，在账号切换或重试边界停止，不强制终止线程。

发送重试继续使用当前语义：`MESSAGE_NOT_OBSERVED_RETRIES = 3` 表示消息接口总共最多请求三次。首次获取的 `roomId`、Session 和会话参数继续复用，后续只重试消息 POST。

## 数据与安全

- 运行数据统一放在 `runtime`，不写入程序安装目录以外的任意位置。
- 账号列表只显示用户名与短 ID，不显示 Cookie。
- 日志保持现有脱敏策略。
- 打包产物不携带开发机账号、浏览器资料或历史报告。
- 原项目与桌面项目使用独立账号库和 Chromium profile。

## 打包与分发

使用 PyInstaller `onedir` 输出一个目录，其中包含主程序 EXE、Qt 运行库、Python 依赖、业务模块及内置 Chromium。启动时根据 `sys._MEIPASS`/可执行文件目录解析资源位置，并将可写数据放在 EXE 同级 `runtime`。

构建脚本执行以下检查：

1. 测试套件通过；
2. 发布目录不存在账号 Cookie、profile 和日志；
3. 内置 Chromium 可启动；
4. EXE 可打开主界面、完成一次嵌入登录流程并读取空账号库。

## 错误处理

- 浏览器不可用：提示内置 Chromium 缺失或损坏。
- 浏览器嵌入失败：终止本次登录并清理进程，避免退化为工具外窗口。
- 登录取消或超时：不保存不完整 Cookie。
- 网络或直播间错误：沿用业务层分类，在界面展示简短原因。
- 监听不可用：终止本次发送并提示检查网络和直播间状态，保持当前业务规则。
- 程序退出：先请求工作线程停止，再关闭 Chromium 和监听连接。

## 测试范围

- 界面字段校验、按钮状态和统计映射。
- 工作线程成功、失败、取消信号。
- 浏览器 PID 到窗口句柄的选择规则，以及窗口嵌入/恢复调用。
- 登录成功、重复账号、取消、超时和浏览器异常。
- 发送总次数、仅重试 POST、失败分类和脱敏日志回归。
- PyInstaller 冒烟测试和发布目录敏感文件扫描。

## 首版不包含

- 在界面中编辑全部高级参数；
- 定时任务和长期后台服务；
- 云端账号同步；
- macOS 或 Linux 支持；
- 安装器和自动更新。
