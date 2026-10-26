# 架构说明

## 当前入口与职责

本文件描述当前独立桌面项目。操作与配置见 [desktop-usage.md](desktop-usage.md) 和 [usage.md](usage.md)；[design.md](design.md) 与 [implementation-plan.md](implementation-plan.md) 保留历史设计/计划，不能替代当前规范。

```text
app.py → desktop.paths / MainWindow
                    ├── LoginWorker → browser_auth → account_store
                    ├── DrissionPage → 独立 Chromium 登录窗口
                    └── SendWorker → runner → client / signing
                                       └── listen_msg.LiveMessageListener
settings.py → config → cli → runner       （保留的命令行入口）
```

Qt 主线程只负责界面状态，业务 QObject 工作线程负责登录和发送，使用信号提供脱敏日志、阶段与统计。登录由 DrissionPage 打开独立 Chromium 窗口。停止标记通过回调在账号/重试边界检查；进行中的请求或监听等待先完成，关闭时继续持有线程直至安全收尾。

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `desktop.paths` | 项目/EXE 资源定位、runtime 目录、随包浏览器优先级 | TikTok 网络请求 |
| `desktop.main_window` / `workers` | 控件状态、线程生命周期与业务桥接 | 签名算法实现 |
| `config.py` | 路径、请求常量、settings 配置映射 | 网络请求 |
| `account_store.py` | 版本 1 JSON、身份去重、原子写入 | 远程会话预校验 |
| `browser_auth.py` | 官方页面登录、Cookie/Ticket Guard 捕获、取消清理 | 发送直播消息 |
| `signing.py` | 加载算法并生成请求签名 | 保存账号 |
| `client.py` | token、直播页、roomId、消息请求与响应判断 | 多账号循环 |
| `runner.py` | 单轮账号调度、监听观察、节流/重试与报告 | Qt 控件 |
| `listen_msg.py` | 直播弹幕连接、缓存与当前内容/身份规则匹配 | 严格发送者身份证明 |
| `cli.py` | 命令行退出码与安全异常边界 | 桌面窗口 |

## 数据与配置

账号库是 `version = 1`、`accounts` 列表的对象。磁盘上的每个账号保存单个 `cookie` 字符串，包含 `id`、`created_at`、`updated_at`、`environment_seed`，还可包含识别到的 `username` 与配套 `ticket_guard`。读取时将字符串解码为内存 `cookies` 数组；兼容旧版数组，下一次原子保存转为单个字符串，不保证持久化浏览器对象的原始属性。Cookie、token 和密钥不进入日志。

身份去重优先使用 `multi_sids` 中唯一可确认的 TikTok 数字 UID，再按已有会话标识兼容匹配。同一账号更新 Cookie，身份不明确时不猜测。损坏库保留原文件；只有消息响应明确要求重新登录才删除对应账号，网络/403/429/WAF 等失败保留账号。

桌面库、profile、报告和日志位于项目或 EXE 同级的 `runtime` 子目录；CLI 保留根目录账号/报告与 `port` 默认路径。浏览器使用随包 Chromium，源码没有随包浏览器时可使用系统 Chrome 或配置的路径。不同入口不自动共享账号库。

高级值从 `settings.py` 映射，桌面用户名/消息来自界面，CLI 来自 `MANUAL_INPUT`。当前默认账号间隔为 `ACCOUNT_REQUEST_INTERVAL_SECONDS = 15` 秒，runner 在第二个及后续不同账号开始前等待；没有首账号前或最终账号后的固定等待。消息观察默认 10 秒、重发间隔 2 秒；监听连接默认每次 15 秒、连接失败后间隔 1 秒。发布版使用构建时配置，修改源码需重建。

## 发送与监听

`MESSAGE_NOT_OBSERVED_RETRIES = 1` 表示默认每条计划消息仅尝试一次，不自动重发；用户可在设置中修改总尝试次数，已有设置保留。监听连接使用独立的 `MESSAGE_OBSERVER_CONNECT_RETRIES = 3`，是首次加三次重试，最多四次连接。取消阻止下次 POST，但不能撤回在途请求。桌面房间调度可选串行（默认，按输入顺序等待清理）或并行（最多同时 3 个房间）；两种模式均在单个房间内并发已选账号。遇到 `4010048` 且明确提示本场设置禁止评论时，房间级停止，不影响其他房间。

监听比较请求开始后的规范化正文；预期用户名可匹配观察到的 `unique_id` 或 `nickname`，或由数字 UID 独立匹配。名称匹配与 UID 匹配是“或”关系，不要求同时一致。其他用户的相同昵称和正文可能误计 `delivered`；该字段表示当前规则命中的事件，不能作为严格身份证明。`success` 和 `delivered` / `acceptedNotObserved` 等计数有重叠，详见当前使用说明。

正常业务收尾原子覆盖失败与禁言报告；早期错误/异常退出可能未刷新报告。日志、报告与账号视图不输出完整 Cookie 或其他凭据。

## 兼容与扩展

`send_live_message.py` 保持旧公开函数与替换点，实际实现位于 `tiktok_live/`。新增字段放入账号存储模块，登录规则放入 `browser_auth`，请求/响应分类放入 `client`，节流、观察和重试策略放入 `runner`，算法放入 `algorithms`。改变监听身份规则需单独评估昵称兼容、UID 匹配与碰撞回归；此次文档修订保持现有业务行为。
