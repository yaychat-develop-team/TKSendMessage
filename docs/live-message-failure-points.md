# 直播间消息请求链路与失败点

本文用于排查桌面工具在“直播间消息发送”过程中出现的失败。重点是判断失败发生在哪个阶段、消息接口是否已经调用，以及应该记录哪些脱敏信息。

> 安全要求：日志和诊断结果不得输出 Cookie、`msToken`、CSRF、完整签名、Ticket Guard 原始数据、完整请求 URL 或账号详情 JSON。

## 1. 完整请求顺序

正式发送顺序如下：

```text
解析目标与账号
  → 查询 roomId 和直播状态
  → 建立独立访客弹幕监听 WSS
  → 为发送账号创建 HTTP Session
  → 刷新 msToken
  → GET /webcast/im/fetch/?client_enter=1
  → 解码首次 protobuf 响应
  → 取得服务端 WSS 地址、route 和 cursor
  → 建立账号 WSS
  → 发送进房帧和持续心跳
  → 等待进房响应并完成预热
  → 获取 CSRF
  → POST 直播聊天接口
  → 保持账号 WSS，等待独立访客端观察弹幕
  → 分类结果并关闭资源
```

任何发送前阶段失败，都必须在聊天 POST 前停止，不能退回静态 WSS 地址、空 cursor 或无心跳发送。

## 2. 房间识别阶段

### 主要操作

- 请求目标主播直播页。
- 必要时处理公开页面 WAF 后在同一 Session 重试。
- 从绑定目标主播的页面状态中提取 `roomId`。
- 页面不可用时使用直播间接口兜底。
- 核对房间所有者和开播状态。

### 常见失败

| 分类 / 现象 | 含义 | 是否调用消息接口 |
| --- | --- | --- |
| `room_state_unconfirmed` | 无法可信确认房间、主播或直播状态 | 否 |
| `room_offline` | 已识别房间，但主播未开播 | 否 |
| `roomExists=False` | 没有取得可用 roomId | 否 |
| `waf_challenge` | 公开直播页仍被 WAF 拦截 | 否 |
| `listener_unavailable` | 访客监听无法完成房间初始化或 WSS 连接 | 当前桌面流程停止发送 |

### 注意事项

- 页面中出现其他推荐直播间 ID，不能作为目标房间 ID。
- 网络错误或 WAF 不能直接解释为“主播未开播”。
- `roomId` 查询成功只证明找到了房间；账号会话由后续 initial fetch 和 WSS 进房响应确认。

## 3. 访客监听 WSS

访客连接使用 `role=delivery_observer`，只负责观察公开弹幕。它与发送账号的 WSS 是两条不同连接。

当前源码的旁观初始化顺序为：独立匿名 check/register → 必要时查询 roomId → 签名并直连 WSS → 空 cursor 进房 → 心跳、接收及 ACK。访客不再请求直播页、令牌 report 或 initial fetch，也不借用发送账号的 Cookie 或 fetch 响应。先尝试通用 `webcast-ws.tiktok.com`，握手失败后在总启动时限内尝试 `webcast-ws.eu.tiktok.com`；两者是经测试的候选地址，并非从页面动态提取。日志显示实际连接的主机和路径，不打印签名查询串。旧的动态旁观路由文档为历史诊断记录，不代表当前默认流程。

### 常见失败

| 分类 / 现象 | 含义 | 是否调用消息接口 |
| --- | --- | --- |
| `listener_connect_failed` | 单次访客连接失败，可按设置重试 | 尚未发送 |
| `listener_unavailable` | 所有访客连接尝试失败 | 桌面流程停止发送 |
| 连接后中途断开 | 不能继续确认公开展示 | 取决于断开时间 |

### 注意事项

- 访客 WSS 成功不代表账号首次 fetch 一定成功。
- 访客 WSS 使用独立匿名 Cookie 直接连接，不依赖 fetch；账号 WSS 仍必须使用发送账号的首次 fetch 结果。
- `listenerUnavailable` 表示无法判断展示，不等于消息一定没有发出。

## 4. Token 和 CSRF 阶段

### `msToken`

发送账号在每次新发送链路中刷新当前运行时 `msToken`。不能把几天前保存的旧 Token 直接当作当前请求 Token。

### CSRF

聊天 POST 前必须取得当前会话的 CSRF Token。

| 分类 | 含义 | 是否调用消息接口 |
| --- | --- | --- |
| Token 刷新异常 | `/web/report` 或对应运行时刷新失败 | 否 |
| `csrf_token_unavailable` | 未取得可用 CSRF | 否 |

### 注意事项

- 只记录 Token 是否存在和长度范围，不记录原文。
- 不同账号、不同线程不能共享 Token 状态。
- 并发房间中的每个账号任务使用独立 HTTP Session。

## 5. 为什么正式流程不请求 `/webcast/room/enter/`

正式发送链路不再调用单独的 `POST /webcast/room/enter/`。建立观众会话所需的
协议入口是首次 `/webcast/im/fetch/` 中的 `client_enter=1`，其 protobuf 响应提供
账号 WSS 必需的 `cursor`、`route` 和 `push_server`；随后账号 WSS 还必须收到
`im_enter_room_resp` 并持续发送心跳。

纯接口实测中，单独的 room-enter POST 可能被风控返回 HTTP 403；同一账号完全
跳过该 POST 后，initial fetch、账号 WSS 和 chat POST 正常完成，独立访客 WSS
按正文、发送者和 message_id 三项一致观察到评论。因此该 POST 既不是 initial
fetch 的前置条件，也不应成为发送阻断点。

```text
直播页/房间接口       → 识别 roomId 和动态 fetch 域名
initial fetch          → client_enter=1，取得 WSS 初始化参数
账号 WSS 进房帧/响应  → 确认实时会话并维持心跳
```

## 6. 首次 `/webcast/im/fetch/`

这是账号 WSS 初始化的关键接口。请求主机不再固定写死。工具先从当前主播的
直播页文档中提取并校验 `webcast*.tiktok.com`，再请求该主机的固定路径：

```text
GET https://<直播页给出的 webcast 主机>/webcast/im/fetch/
```

页面没有给出可用 endpoint 时，才回退到
`https://webcast.tiktok.com/webcast/im/fetch/`。不能根据账号地区、主播语言或
Cookie 中的国家字段自行猜测区域域名。

参数按网页形状编码：

```python
urlencode(params, doseq=True)
```

首次 HTTP fetch 只发送一个直播版本号：

```text
version_code=270000
```

每次尝试都重新构造 URL 和签名。Cookie 通过 `cookies=`/Cookie jar 传递，不手工拼接显式 `Cookie` 请求头。

### 完整查询参数及用途

当前首次 fetch 使用以下有序参数。参数顺序和最终编码后的字节串会参与签名，因此构造完成并签名后不能再增删、重排或改写参数。

| 参数 | 当前值 / 来源 | 后续用途与注意事项 |
| --- | --- | --- |
| `version_code` | `270000` | 首次 HTTP fetch 只发送一次。2026-10-23 同账号、同 UA 的交替测试中，额外附加 `180800` 可导致 403 或空 JSON；此结论不要求修改 WSS 自己的参数。 |
| `device_platform` | `web` | 表示 Web 客户端身份。 |
| `cookie_enabled` | `true` | 声明浏览器启用 Cookie；实际请求也必须携带账号 Cookie jar。 |
| `screen_width` | 每次 fetch 从常见分辨率池成对抽取 | 当前池：1920×1200、2560×1440、1366×768、1536×864、1280×720、3840×2160。一次请求只抽取一对，之后再签名；随机结果允许连续相同。 |
| `screen_height` | 与宽度配套抽取 | 不独立随机宽高；本项只描述 HTTP fetch，不改变已有 WSS 握手参数。 |
| `browser_language` | `zh-CN` | 浏览器语言；应与整套请求环境保持一致。 |
| `browser_platform` | `Win32` | Windows Web 平台标识。 |
| `browser_name` | `Mozilla` | 网页请求使用的浏览器名称字段。 |
| `browser_version` | 从当前保存账号 UA 派生，例如 `5.0 (...) Chrome/145.0.0.0 ...` | 必须与请求头 UA、签名 UA 属于同一浏览器身份；按网页字段格式不重复 `Mozilla/` 前缀。 |
| `browser_online` | `true` | 声明当前浏览器在线。 |
| `tz_name` | `Asia/Shanghai` | 当前验证配置的时区环境；不能与其他指纹字段随意拼接成矛盾组合。 |
| `ws_direct` | `1` | 请求服务端返回可直接用于 WSS 初始化的数据。 |
| `aid` | `1988` | TikTok Web 应用标识。 |
| `app_name` | `tiktok_web` | TikTok Web 客户端名称。 |
| `live_id` | `12` | Webcast 直播业务标识。 |
| `app_language` | `zh-Hans` | 应用语言。 |
| `client_enter` | `1` | 表示客户端正在执行进房初始化。 |
| `room_id` | 当前目标直播间 ID | 必须来自已核对主播和直播状态的当前房间，不能使用推荐房间 ID。 |
| `identity` | `audience` | 当前账号以观众身份进入。 |
| `history_comment_count` | `6` | 请求有限数量的历史评论；历史评论不用于确认本次消息展示。 |
| `fetch_rule` | `1` | 首次拉取规则。 |
| `last_rtt` | `-1` | 首次连接前没有可用 RTT。 |
| `cursor` | `0` | 这是首次 HTTP fetch 的输入起点；不要与响应中服务端生成的 cursor 混淆。 |
| `internal_ext` | `0` | 首次 HTTP fetch 的初始扩展状态；后续 WSS 帧可能返回新的 `internal_ext`。 |
| `sup_ws_ds_opt` | `1` | 声明支持当前 WebSocket 数据流选项。 |
| `resp_content_type` | `protobuf` | 要求服务端返回 protobuf，而不是 JSON。 |
| `did_rule` | `3` | 当前 Web 设备标识规则。 |
| `webcast_language` | `zh-Hans` | Webcast 消息流语言。 |

编码后的开头必须类似：

```text
version_code=270000&device_platform=web&...
```

错误形式包括：

```text
version_code=%5B%27180800%27%2C+%27270000%27%5D
version_code=180800%2C270000
```

### 请求头、Cookie 与签名顺序

首次 fetch 的构造顺序必须固定：

```text
从当前直播页文档提取并验证 fetch endpoint
  → 构造有序参数字典
  → urlencode(..., doseq=True)
  → 得到完整未签名 URL
  → 使用本次 fresh msToken 和同一 UA 重新签名
  → 生成与签名 UA一致的请求头
  → 新建未参与直播页/WAF 请求的隔离 HTTP Session
  → 通过 Cookie jar 发送 GET
```

主要请求头用途：

| 请求头 | 用途 |
| --- | --- |
| `User-Agent` | 必须与签名 UA、`browser_version` 对应。 |
| `sec-ch-ua` | 与 Chrome 主版本保持一致。 |
| `Origin: https://www.tiktok.com` | 表示请求来自 TikTok Web 页面。 |
| `Referer: https://www.tiktok.com/@<username>/live` | 绑定当前目标主播页面，用户名必须使用当前解析目标。 |
| `Sec-Fetch-Dest: empty` | 对应网页 XHR/fetch 请求。 |
| `Sec-Fetch-Mode: cors` | 跨站 Webcast 请求模式。 |
| `Sec-Fetch-Site: same-site` | `www.tiktok.com` 到 `webcast*.tiktok.com` 属于 same-site 请求。 |

Cookie 使用当前账号 Cookie jar，并把本次刷新的 `msToken` 写入 jar。不要把同一批 Cookie 再拼成显式 `Cookie` 请求头；受控对照中，显式头曾使相同账号、房间和参数从 HTTP 200 变为 403。

首次 fetch 必须使用新的 HTTP Session。直播页 WAF、公开页重试和 Token 刷新会
改变原 Session 的 Cookie/连接状态；继续复用该 Session，曾导致同一账号、
同一房间、同一 Token 的 fetch 从 protobuf 200 变成 403。隔离 Session 仍使用同一
账号 Cookie jar、本次 `msToken`、同一传输指纹和 UA，只隔离前序请求留下的状态。

任何以下变化都要求从未签名 URL 开始重新生成签名：

- fetch 主机名变化。
- `room_id` 变化。
- `msToken` 变化。
- UA 或 `browser_version` 变化。
- 任意查询参数的值或顺序变化。
- 重试发生在新的时间窗口。

### 地区域名与服务端调度

TikTok 的 HTTP Webcast 域名和 WSS 推送域名可能随直播间地区、账号地区、出口 IP、Cookie 环境及服务端调度而不同。不要把“美国、欧洲、亚洲”简单永久写死为一对一映射；应优先使用当前网页或服务端响应给出的实际结果。

本项目和受控对照中出现过的域名包括：

| 类型 | 已观察到的域名示例 | 用法 |
| --- | --- | --- |
| 通用 HTTP fetch | `webcast.tiktok.com` | 当前正式代码的默认 `/webcast/im/fetch/` 主机，部分直播间成功。 |
| 美国 HTTP fetch | `webcast.us.tiktok.com` | 美国/US 调度的网页请求中可能出现；正常网页的 fetch 曾实际发往该域名。不能只因主播名称或语言就断定必须使用它。 |
| 通用 WSS push | `webcast-ws.tiktok.com` | 首次 fetch 响应可能返回的服务端 WSS 主机。 |
| 欧洲 WSS push | `webcast-ws.eu.tiktok.com` | 受控响应中出现过的欧洲推送节点；必须使用响应原值，不能根据 HTTP fetch 域名自行拼接。 |

HTTP fetch 域名与最终 WSS push 域名是两个不同层次：

```text
HTTP fetch endpoint
  → 服务端进行区域/房间调度
  → protobuf 返回 push_server
  → 客户端使用该 push_server 建立 WSS
```

例如，从 `webcast.tiktok.com` 请求 fetch，也可能收到区域化 WSS 节点；反过来，不能因为期望欧洲 WSS 就自行把 fetch 改成一个猜测的欧洲域名。

当前实现状态：

- 直播页解析器从当前页面文档提取 `webcast*.tiktok.com`，拒绝非 HTTPS、端口、认证信息、错误路径和仿冒域名。
- 请求构造器允许并校验合法的 `webcast.<区域>.tiktok.com` 多级区域主机。
- 正式发送链路优先使用页面实际给出的 endpoint，仅在页面未提供时回退通用域名。
- 首次 fetch 使用独立的全新 curl_cffi Session，不复用直播页 WAF/进房 Session。
- 最终账号 WSS 主机不写死，始终使用 fetch protobuf 返回的 `push_server`。
- `push_server` 的 scheme、域名、端口、query 和房间绑定会在连接前校验。

当出现“其他直播间成功、某个直播间稳定 `initial_fetch_unavailable`”时，地区域名是需要验证的单变量之一。推荐的安全选择顺序是：

1. 优先使用当前 TikTok 直播页网络请求实际采用、且位于允许列表内的 fetch 主机。
2. 如果进房/bootstrap 响应提供了可信区域信息，使用经过允许列表验证的对应主机。
3. 没有动态证据时使用通用 `webcast.tiktok.com`。
4. 通用主机出现明确的 HTTP/区域拒绝时，最多进行一次有界的已知区域主机对照，例如美国 `webcast.us.tiktok.com`。
5. 每切换一次主机，都必须重新生成完整 URL 和签名；不能复用通用主机生成的已签名 URL。
6. 任一主机成功返回有效 protobuf 后立即停止域名尝试，使用响应中的 WSS 地址、route 和 cursor。

不得实施：

- 对未知域名无限轮询。
- 根据主播用户名、昵称或消息语言猜地区。
- 把一个房间成功返回的 route/cursor 复用给另一个房间。
- 把美国 fetch 域名强制用于所有美国主播或所有账号。
- 把 `webcast-ws.eu.tiktok.com` 等 WSS 主机当作 HTTP fetch 主机。

### 浏览器身份一致性

以下三处必须属于同一个浏览器身份：

- HTTP 请求头 UA
- 签名函数使用的 UA
- `browser_version`

当前实现按每个保存账号的 UA 选择对应请求身份。`browser_version` 按网页字段格式表示，与该账号的请求 UA 和签名 UA 对应，不能全局写死某个 Chrome 版本。

### 成功条件

只有同时满足以下条件，首次 fetch 才算成功：

1. HTTP 200。
2. 没有重定向。
3. `Content-Type` 为 `application/x-protobuf`。
4. 响应体非空且不超过 2 MiB。
5. Protobuf 可以安全解码。
6. 存在非空 cursor。
7. 存在合法的服务端 WSS 地址。
8. 存在可用 route 参数，例如 `imprp`、`wrss`。

### 响应字段及后续用途

首次 fetch protobuf 不是普通的“历史弹幕查询结果”，而是账号 WSS 的初始化结果。当前解码和使用关系如下：

| protobuf 字段号 | 解码结果 | 后续用途 |
| ---: | --- | --- |
| `1` | 历史消息列表 | 当前只统计 `historical_message_count`，不用于本次消息展示确认，避免把旧的相同正文误判为新消息。 |
| `2` | `cursor` | 写入账号 WSS 的 `im_enter_room` protobuf 进房帧字段 6；不能改成空字符串或输入参数中的初始 `0`。 |
| `5` | fetch `internal_ext` | 当前保存备用和诊断；不直接替代后续 WSS 帧自己的 `internal_ext`。 |
| `7` | repeated route 子消息 | 每个子消息的字段 1为 key、字段 2为 value；例如 `imprp`、`wrss`，原样加入 WSS URL。 |
| `10` | `push_server` | 作为账号 WSS 基础地址，经过目标校验后与 route 和客户端参数组合。 |

响应字段的具体传递链：

```text
push_server
  └─→ 账号 WSS 基础地址

route_params（imprp / wrss 等）
  └─→ WSS URL 查询参数

cursor
  └─→ encode_enter_room(room_id, cursor=cursor)

fetch internal_ext
  └─→ 保存备用，不作为当前首次 ACK 的固定值

历史消息
  └─→ 只统计数量，不参与实时展示判断
```

WSS 建立后，服务端 `msg`/`im_enter_room_resp` 帧可能再次返回 `internal_ext` 和 `need_ack`。如果该帧需要 ACK，必须使用该帧自己的 `log_id + internal_ext` 编码 ACK，不能盲目使用首次 fetch 保存的 `internal_ext`。

首次 fetch 请求本身也可能在服务端产生状态：服务端会结合账号 Cookie、fresh `msToken`、roomId、IP、地区和浏览器身份分配 cursor、route 和 WSS 节点。因此，即使事先知道一个旧 WSS 地址，也不能跳过 fetch 或复用旧响应。

### `initial_fetch_unavailable`

这个分类表示首次 fetch 没有得到可用于账号 WSS 的有效结果。它是一个汇总分类，可能对应：

| 具体失败点 | 说明 |
| --- | --- |
| 网络异常 | DNS、TLS、代理、超时或连接中断 |
| HTTP 403/429/其他非 200 | 请求被风控、限流或服务器拒绝 |
| 重定向 | 返回了当前实现不接受的跳转 |
| 空响应 | HTTP 层返回但没有 protobuf 内容 |
| Content-Type 错误 | 返回 HTML、JSON、挑战页或其他内容 |
| 响应过大 | 超过 2 MiB 安全限制 |
| Protobuf 截断/非法 | wire-format 无法安全解析 |
| 缺少 cursor | 无法构造正确进房帧 |
| 缺少 push server | 无法确定账号 WSS 地址 |
| 缺少 route | 无法构造服务端认可的 WSS 路由 |

出现该分类时：

- 账号 WSS 尚未启动。
- 聊天 POST 尚未调用。
- 访客 WSS 可能已经连接成功，但只负责旁观。
- 工具按配置完成首次请求和重试后停止。

## 7. 为什么其他直播间成功、某个直播间失败

同一账号、同一程序、同一网络下，其他直播间成功，可以排除“账号整体失效”“签名算法完全不可用”和“网络完全不可用”，但不能证明所有房间都使用同一服务端准入规则。

房间之间可能存在以下差异：

1. **区域域名和集群不同**：网页可能按房间和出口 IP 使用 `webcast.tiktok.com`、`webcast.us.tiktok.com` 或其他区域节点；当前账号 fetch 固定使用通用域名，这是一项需要验证的差异。
2. **房间级准入不同**：服务端可能根据账号地区、Cookie 地区、IP、房间集群和当前会话状态决定是否发放 cursor/route。
3. **响应结构不同**：HTTP 200 protobuf 也可能缺少当前实现要求的路由字段，或采用另一种字段组合。
4. **临时风控或限流**：同一房间连续快速初始化可能被单独限制。
5. **并发状态竞争**：同一账号并发进入多个房间可能影响 Token 或服务器状态；如果单房间重复测试仍稳定失败，并发就不是唯一原因。

判断时应比较同一轮中的阶段结果，而不是只比较最终成功/失败：

| 对比项 | 成功房间 | 失败房间 |
| --- | --- | --- |
| roomId/直播状态 | 是否成功 | 是否成功 |
| 访客 WSS | 是否连接 | 是否连接 |
| fetch host | 实际域名 | 实际域名 |
| fetch HTTP 状态 | 状态码 | 状态码 |
| Content-Type/长度 | 类型和字节数 | 类型和字节数 |
| protobuf 字段 | cursor/server/route | 缺失项 |
| 账号 WSS | 是否启动并预热 | 是否启动 |
| 聊天 POST | 是否调用 | 是否调用 |

## 8. 首次 fetch 的安全诊断字段

为了区分 403、空响应和 protobuf 缺字段，建议记录以下脱敏元数据：

```text
fetch_host
attempt_index
http_status
content_type
response_length
redirected
redirect_host（如有，仅主机名）
decode_stage
has_cursor
cursor_length
has_push_server
push_server_host（仅主机名）
route_keys
historical_message_count
error_type（固定类型名）
```

不得记录：

```text
完整 signed URL
完整 query string
X-Bogus/签名原文
msToken 原文
Cookie 原文
cursor 原文
route value 原文
完整 push_server URL
protobuf 原始响应
```

建议的 `decode_stage` 固定值：

```text
request
http_status
content_type
body
protobuf
cursor
push_server
routes
complete
```

## 9. 账号 WSS 初始化

首次 fetch 成功后，工具使用服务端返回的：

- `push_server`
- route 参数
- cursor

构造 `role=account_heartbeat` 的账号 WSS。不能使用访客 WSS 替代账号 WSS。

### 常见失败

| 现象 | 含义 | 是否调用消息接口 |
| --- | --- | --- |
| WSS HTTP upgrade 失败 | 服务端拒绝握手、网络/代理异常 | 否 |
| upgrade 后立即关闭 | WSS 参数、route、身份或服务端状态不接受 | 否 |
| `im_enter_room_resp` 缺失 | 没有确认账号进入直播间 | 否 |
| 心跳发送失败 | 连接不可持续 | 否或停止后续发送 |
| 预热超时 | 心跳次数、时间跨度或进房响应不足 | 否 |

### 预热条件

默认至少满足：

- 收到服务端进房响应。
- 成功发送至少 3 次目标房间心跳。
- 心跳跨度至少为两个心跳间隔。
- WSS 在整个预热和发送观察阶段保持连接。

## 10. 聊天 POST

只有账号 WSS 预热完成后才调用聊天接口。每条消息都重新生成：

- 当前时间戳。
- 请求签名。
- Ticket Guard 请求头。
- 当前消息请求体。

### 常见结果

| 分类 | 含义 |
| --- | --- |
| `success` | HTTP 和业务状态明确成功 |
| `comment_delivery_uncertain` | 返回不确定，需要旁观端确认 |
| `account_silenced` | 账号疑似被直播间禁言，不连续重试 |
| 登录失效分类 | 平台明确要求重新登录，才移除账号 |
| `send_exception` | 网络、解析或未分类异常 |

### 注意事项

- HTTP 200 不等于公开展示。
- 返回消息 ID 时，旁观端确认应匹配同一消息 ID。
- 明确失败、禁言和登录失效不应通过快速连续重发验证。

## 11. 展示确认

聊天 POST 返回后，账号 WSS继续保持，访客 WSS等待对应公开弹幕。

| 展示分类 | 含义 |
| --- | --- |
| `delivered` | 观察到符合正文、发送者规则和消息 ID 的事件 |
| `acceptedNotObserved` | 接口接受，但观察期内没有找到匹配事件 |
| `listenerUnavailable` | 监听不可用，无法判断是否展示 |
| `send_failed` | 在消息发送前或发送阶段失败 |

必须区分：

```text
接口成功 ≠ 公开展示成功
未观察到 ≠ 一定没有展示
访客监听成功 ≠ 账号 fetch 成功
chat HTTP 200 ≠ 评论一定公开展示
```

## 12. 快速排查决策表

```text
没有 roomId
  → 检查直播页/WAF/接口兜底/主播绑定

访客 WSS 失败
  → 检查访客初始化、网络、代理、房间状态

initial_fetch_unavailable
  → 先看 fetch HTTP 状态、类型和长度
     → 非 200：检查域名、地区、风控、签名身份
     → 200 但非 protobuf：检查挑战页/重定向
     → 200 protobuf：检查 cursor/push_server/route

fetch 成功但账号 WSS 立即关闭
  → 检查是否完整使用服务端 route/cursor 和全量 WSS 参数

账号 WSS 未预热完成
  → 检查进房响应、心跳次数、时间跨度和中途断线

聊天 POST 成功但未观察到
  → 检查评论过滤、重复内容、禁言、观察超时和消息 ID
```

## 13. 重试原则

- 发送前的临时网络故障可以有界重试。
- 每次 fetch 重试必须重新生成签名。
- HTTP 403/429 不应无限快速重试。
- `account_silenced` 不应连续重发。
- 消息 POST 已成功但未观察到时，额外发送可能产生重复弹幕。
- 多次稳定复现同一房间 fetch 失败时，应先补充脱敏诊断，而不是继续盲目重试。

## 14. 当前已确认与尚未确认

### 已确认

- 完整链路在用户授权的在线直播间完成过端到端展示验证。
- 同一条消息的接口返回 ID 与独立旁观事件 ID 一致。
- `doseq=True`、Cookie jar、账号 UA 一致性以及服务端 route/cursor 可以成功建立账号 WSS。
- 不调用单独 room-enter POST 时，`client_enter=1` 的 initial fetch、账号 WSS 和 chat POST 可以完成端到端展示。
- 账号 WSS 必须持续覆盖聊天 POST 和展示观察阶段。

### 尚需按失败房间确认

- 失败房间实际 fetch HTTP 状态和 Content-Type。
- 是否需要根据当前直播页文档动态选择区域 webcast 域名。
- HTTP 200 时是否返回了不同的 protobuf 路由结构。
- 房间级地区、账号地区或风控准入是否造成差异。

在获得上述脱敏证据前，应使用“首次 fetch 未取得有效初始化响应”描述问题，不应直接断言为签名错误、房间关闭或账号失效。
