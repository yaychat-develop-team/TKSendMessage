# 动态旁观路由与 fetch 复测（2026-10-22）

## 本次变更

旁观端不再使用固定欧洲 WSS 域名。无浏览器、无发送账号 Cookie 的独立匿名会话执行：

1. `/ttwid/check/`，需要时 `/ttwid/register/`，取得匿名访客 Cookie。
2. 未提供 room_id 时先通过房间查询接口确定当前房间。
3. GET 当前主播 `/@username/live` 文档，校验主播、当前 room_id 和开播状态，从文档提取 fetch endpoint。
4. 在同一匿名会话刷新令牌，使用同一 UA 生成签名和 browser_version。
5. 请求该动态 endpoint 的 `/webcast/im/fetch/`，要求 HTTP 200、protobuf 类型且能够解码。
6. 用响应的 `push_server` 原始主机、路由参数和 cursor 初始化 WSS；路径仍为 `/webcast/im/ws_proxy/ws_reuse_supplement/`，不根据地区自行猜主机。

获取不到路由时明确失败，不回退固定 EU 域名。连接期间仍保留原来的 enter 帧、心跳和 ACK 处理。

Cookie 按实际 fetch/WSS 目的域名和路径选择，仅允许匿名访客字段，检查 secure、过期时间以及 host-only 作用域。发现登录 Cookie 即终止匿名初始化。HTTP 请求限制响应大小、禁止重定向，并受总启动期限和取消信号约束。

发送账号的初始 fetch 参数构造、Cookie 和签名流程未因本次路由改动而改变；共享构造器只新增匿名旁观角色及其凭据校验。

## 实际复测

房间 `min140962`，room_id `7699467757708118791`，文档解析地区 SG、fetch 主机 `webcast.tiktok.com`。

| 账号 | fetch HTTP | 内容类型 | 字节数 | 解码 |
| --- | --- | --- | ---: | --- |
| boaluodyblala1 | 200 | application/x-protobuf | 39564 | cursor 和 2 个路由参数 |
| spider_king017 | 200 | application/x-protobuf | 56637 | cursor 和 2 个路由参数 |
| djkfypujiwr | 200 | application/x-protobuf | 37313 | cursor 和 2 个路由参数 |
| uhovwkpq102 | 200 | application/x-protobuf | 66047 | cursor 和 2 个路由参数 |
| louisduir8x | 200 | application/x-protobuf | 45205 | cursor 和 2 个路由参数 |

这轮每个账号均使用自身保存的 Chrome 145 UA，逐请求验证头部 UA、签名 UA、browser_version 对应一致；fetch 查询令牌和发送 Cookie 中的新令牌一致。没有发送评论，没有混用其他账号 Cookie。

匿名旁观实测 `available=true`、`failure_stage=null`，服务端返回并实际连接的主机为 `webcast-ws.tiktok.com`。随后关闭监听。

## 尚未确定的问题

之前相同房间、同一账号请求曾出现 HTTP 403 空响应，或 HTTP 200 但 JSON 类型且零字节；本轮在没有修改发送端 fetch 构造的情况下全部恢复，不能把恢复归因于旁观路由改动。

当前证据不能确定失败根因，也不能据此判定账号过期或永久失效。此前单变量实验提示页面更新的 tt_chain_token 可能改变结果，但不足以解释所有账号差异。禁止把匿名成功当成登录账号成功、把 HTTP 200 空响应当成有效 fetch。

再次失败时应记录脱敏的阶段、HTTP 状态、内容类型、长度、动态 endpoint 来源和 UA/token 一致性，保留对照成功请求的时间；不要盲目更换域名、地区或跨账号 Cookie。fetch 成功只证明取得初始化信息，不等于消息已展示；本轮未验证评论展示。

本次源码改动已打包并覆盖 `dist/TKChatWave.exe`，账号及配置目录未改动。
发布前冻结版 328 项测试通过，隔离启动的界面、内置浏览器、空账号及依赖检查全部通过。
发布 SHA-256：`0ef83834b463d31a75d922818b685c6bea726c76e8b1d1e0632105837613bae7`。
旧 EXE 备份：`build/work/release-observer-20261022-previous.exe`。

## 离线验证

- 完整 `unittest discover -s tests`：644 项通过，196.369 秒，日志 `build/work/observer-full-final.log`。
- 路由、Cookie 隔离、初始化及心跳定向回归：34 项通过。
- 第一轮全量测试暴露 1 个依赖旧固定域名的旁观心跳夹具；补齐服务端初始路由模拟后重新全量通过，没有恢复生产代码中的固定域名兜底。
- 独立只读代码审查未发现阻塞问题。
- `client.py`、`runner.py`、`parallel_runner.py`、`signing.py` 与之前发布源码的文件哈希一致。
