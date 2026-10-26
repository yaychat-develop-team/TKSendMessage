# 脚本与业务使用说明

桌面发布版的主入口是 `TKChatWave.exe`，源码桌面入口为 `python app.py`。两者需要勾选发送账号、填写目标用户名与消息；登录时打开独立 Chromium 窗口，并使用 `runtime` 资料目录，详见 [桌面使用说明](desktop-usage.md)。以下说明保留的 CLI / Python 业务入口，与桌面默认路径不同。

## 安装和启动

需要 Python 3.10 或更高版本：

```powershell
python -m pip install -r requirements.txt
python send_live_message.py
# 或
python -m tiktok_live
```

CLI 和桌面登录都使用 DrissionPage。源码模式可通过系统 Chrome 或 `TIKTOK_BROWSER_EXECUTABLE` 指定浏览器；桌面发布版包含 Chromium，无需安装系统 Chrome。登录浏览器是独立窗口。不要混淆 CLI 的 `port` / 根目录账号库与桌面的 `runtime`。

## CLI 配置

编辑 `settings.py` 中的 `MANUAL_INPUT`，填写自己的具体获授权目标和消息：

```python
MANUAL_INPUT = {"username": "creator", "message": "你好"}
ADD_NEW_ACCOUNT = False
ROOM_ID_COOKIE = ""
```

`ADD_NEW_ACCOUNT = True` 强制登录，保存后继续只使用刚登录的账号发送；`False` 读取所有去重后的已保存账号。缺失/空库时自动登录并继续发送。只验证登录保存时应单独调用登录函数，不进入发送主流程：

```powershell
python -c "from tiktok_live.browser_auth import login_new_account; from tiktok_live.config import ACCOUNT_STORE_PATH; login_new_account(ACCOUNT_STORE_PATH)"
```

此调用仍需要用户在官方页面交互登录，不输出返回账号或 Cookie。官方页面可提供扫码、手机号、邮箱、Google、Facebook 等方式；程序等待返回 TikTok 页面以及稳定完整的关键 Cookie 和 `s_v_web_id`。默认登录上限 300 秒，未完成/取消/超时不写入不完整账号。

桌面输入取自界面，不需要改 `MANUAL_INPUT` 或 `ADD_NEW_ACCOUNT`；高级默认值仍从打包时的 `settings.py` 读取，修改源码后需重新构建发布版。当前默认值：

| 配置 | 默认值 / 含义 |
| --- | --- |
| `LOGIN_TIMEOUT_SECONDS` | 300 秒 |
| `ACCOUNT_REQUEST_INTERVAL_SECONDS` | 15 秒，账号间隔 |
| `MESSAGE_OBSERVATION_ENABLED` | True，启用弹幕确认 |
| `SHOW_LIVE_COMMENTS_IN_CONSOLE` | False，隐藏逐条弹幕，仍然监听 |
| `MESSAGE_OBSERVATION_TIMEOUT_SECONDS` | 10 秒，每次发送后的观察时间 |
| `MESSAGE_NOT_OBSERVED_RETRIES` | 默认 1，只发送一次、不自动重发；表示消息 POST 总尝试次数，包含首次，小于 1 按 1 处理。需要重试可在工具设置中修改。 |
| `MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS` | 2 秒 |
| `MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS` | 15 秒，每次连接的等待上限 |
| `MESSAGE_OBSERVER_CONNECT_RETRIES` | 3，首次连接之后最多三次重试，最多四次连接 |
| `MESSAGE_OBSERVER_RETRY_DELAY_SECONDS` | 1 秒 |
| `ROOM_ID_COOKIE` | 空，通常无需配置；只能为直播间识别兜底，不替代当前发送账号身份 |

## 调度、监听与结果

每次只遍历一轮，每个不同账号一个最终统计结果。正式发送顺序是：刷新本次账号令牌 → 识别房间 → 获取 CSRF → HTTP 进房 → 首次 `/webcast/im/fetch/` → 使用服务端返回的 WSS 地址、路由参数和 cursor 建立账号连接并完成进房/心跳预热 → 消息 POST。首次 fetch 使用 `urlencode(..., doseq=True)`，只发送一次 `version_code=270000`；每次请求重新签名，请求头 UA、签名 UA 与 `browser_version` 使用同一个值。首次 fetch 或账号 WSS 初始化失败会在消息 POST 前终止，不退回静态地址、空 cursor 或无心跳发送。

发送前 token / 房间识别的异常可按业务实现重试；消息总尝试次数默认 1，不自动重新 POST。用户可在桌面设置中调整总尝试次数，已有设置保留；重试仍受业务失败、禁言、房间禁评和取消规则限制。监听连接失败后仍继续发送并记 `listenerUnavailable`，不会为缺监听自动重发。`4010048` 且明确提示“由于本场直播的设置，无法发布评论”时停止该房间后续发送，不观察或重试该响应，不影响其他房间。

登录完成时会读取浏览器实际 UA 并保存到对应账号，后续该账号的 `msToken`、直播页、签名和消息请求始终复用这套 UA/请求身份，不再随机选择。设备环境由环境种子稳定选择，`ttwid` 取自当前账号 Cookie。每一次消息 POST（含重试）均根据该账号的 Ticket Guard 原始状态、目标接口路径和当前时间重新生成签名请求头，不再复用登录时保存或迁移生成的缓存请求头。状态缺失、不完整或不能生成有效请求头时，以 `ticket_guard_unavailable` 停止该账号的消息 POST，保留账号并提示重新登录。

`processed = success + failed + expired + temporaryError`。`delivered` 是找到正文及当前名称或 UID 规则匹配的弹幕；名称规则包含 nickname，且名称命中不要求 UID 一致，存在同昵称误判，详见[桌面匹配说明](desktop-usage.md#发送次数与展示确认)；`acceptedNotObserved` 是重试/等待后未观察到；这两项和 `listenerUnavailable` 是附加展示计数，与处理计数重叠。接口成功不保证公开展示；不确定评论响应只有被观察到才计成功，否则计失败。`account_silenced`（识别到的禁言响应）记入失败和禁言报告，不重试。完整分类、403/网络、重复评论与静音排障见 [桌面说明](desktop-usage.md#排障)。

完整请求顺序、每个前置失败点以及首次 fetch 的脱敏诊断要求，见 [直播间消息请求链路与失败点](live-message-failure-points.md)。

停止是桌面传入取消回调时的协作行为：当前请求/监听结束后阻止下一账号或重试，保留完成结果，不撤回已发消息。CLI 没有桌面停止按钮，不应把 Ctrl+C 当作相同的报告收尾保障。

## 账号与报告路径

| 数据 | CLI 默认位置 | 桌面位置（项目或 EXE 同级） |
| --- | --- | --- |
| 账号库 | `tiktok_accounts.json` | `runtime/accounts/tiktok_accounts.json` |
| 登录 profile | `port/<随机本机端口>/` | `runtime/profiles/<随机本机端口>/` |
| 失败报告 | `failed_accounts.json` | `runtime/reports/failed_accounts.json` |
| 禁言报告 | `silenced_accounts.json` | `runtime/reports/silenced_accounts.json` |
| 桌面轮转日志 | CLI 主要写终端 | `runtime/logs/desktop.log` |

账号库原子写入，保存单个 Cookie 字符串与环境种子，并兼容旧 Cookie 对象数组。同一可确认 TikTok UID 更新而不重复追加；不能确认唯一 UID 时保留既有身份匹配规则。原项目、桌面和 CLI 不自动共享账号库。明确重新登录响应才删除对应账号；网络、风控或其他失败不会删号。损坏库明确报错并保持原文件，应先私下备份再恢复。

正常完成的失败/禁言报告原子覆盖，分别使用 `{"failed_accounts": []}`、`{"silenced_accounts": []}` 表示无记录。早期错误或异常退出可能仍保留上轮报告；查看时间和日志。报告只保留短 ID、用户名、分类及禁言业务状态码，不含 Cookie。

Cookie、`msToken`、CSRF、签名与 Ticket Guard 私密值不应被打印或打包。账号库和 profile 是私密运行数据；消息正文也可能进入日志，勿填写凭据。发布构建扫描源码/浏览器输入与最终目录，不能分发已使用后的 runtime。

## 离线检查和交互验收

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -X faulthandler -m unittest discover -v
python -m compileall -q app.py desktop tiktok_live build tests
python build/verify_release.py dist/TKChatWave
python build/smoke_release.py dist/TKChatWave
```

完整离线回归不会向 TikTok 发送消息。发布冒烟在一次性副本启动冻结 GUI，以及本地 `about:blank` Chromium/CDP；不是登录或发送验收。真实登录、OAuth 新窗口、账号保存与发送需要用户交互。发送测试必须先明确目标、消息与账号数量；默认每账号最多三次 POST，不应称为“只发送一次请求”。

Python 兼容导入保持可用：

```python
from send_live_message import send_live_message
# 或
from tiktok_live import send_live_message
```

`send_live_message(username, cookie, message)` 不管理账号库；结果可能含原始响应，异常也可能携带上游内容。不要直接打印整个返回值、账号对象或异常；只输出允许的分类、数字状态和计数。模块关系见 [architecture.md](architecture.md)。
