# 访客监听取消 initial fetch

## 变更范围

仅旁观监听 `delivery_observer` 默认改为匿名 WSS 直连。发送账号仍执行自身的令牌刷新、initial fetch、账号 WSS 和消息接口流程；缺少有效 initial fetch 时不会继续账号 WSS。

访客流程：

1. 独立匿名 `/ttwid/check/`，需要时 `/ttwid/register/`。
2. 已传入 roomId 则直接使用；否则查询 `/api-live/user/room/` 并验证房主和开播状态。
3. 根据访客 UA 生成当前连接的 WSS 参数及签名，不读取账号 Cookie。
4. 尝试通用 WSS，握手失败且启动时限尚未耗尽时尝试 EU WSS。
5. 连接后发送空 cursor 的进房帧，持续心跳，解码消息并按帧要求 ACK。

候选地址为经过测试的 `webcast-ws.tiktok.com` 与 `webcast-ws.eu.tiktok.com`，路径为 `/webcast/im/ws_proxy/ws_reuse_supplement/`。这两个候选并非页面动态发现；账号 WSS 仍采用账号 fetch 响应中的服务端地址。连接成功日志显示实际选中的主机和路径，不显示签名查询串。

访客不再请求直播页、令牌 report 和 `/webcast/im/fetch/`。保留旧路由模块用于独立测试及兼容显式提供初始化响应的调用，不由默认访客启动调用。

## 正式代码只读实测

2026-10-23，在 `typicalgamer` 上使用正式 `LiveMessageListener`，未替换初始化或 WSS 构造代码；HTTP 观察器拒绝 check/register/room query 之外的请求。

- 持续连接 90.31 秒，访客 fetch 请求为 0。
- 实际连接通用 WSS，收到 11 条去重弹幕，其中连接 10 秒后收到 4 条。
- 三个连续 30 秒区间收到 9、1、1 条弹幕。
- 发送/收到心跳各 10 次，收到消息帧 116 个、发送 ACK 116 次，解析错误 0。
- 不发送测试评论，不保存弹幕内容、消息 ID、Cookie 或签名 URL。

这证明该次运行中无需访客 fetch 也能持续接收公开弹幕，不代表所有地区、房间、网络或未来服务端版本永远支持相同直连配置。访客连接失败仍按原有监听失败处理，不把账号接口成功误报为公开展示成功。
