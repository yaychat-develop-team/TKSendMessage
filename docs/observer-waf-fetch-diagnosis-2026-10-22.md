# 旁观 WAF 修复与 fetch 单变量对照

## 范围

仅修复源码中的匿名旁观初始化。未修改账号发送、签名算法或重试策略，未打包或覆盖 EXE；全程纯接口，不发送评论。

## 已确认并修复的问题

新增动态旁观路由原来只请求一次直播页；遇到 WAF HTML 后直接当房间页面解析，缺失 SIGI_STATE 导致失败。这与账号 fetch 的 403/空响应是两个独立问题。

修复复用 `public_room._public_waf_cookies`：同一匿名 AsyncSession、同一 UA，最多三次页面 GET，证明计算和 HTTP 均受总启动期限及取消约束。只支持项目已有模板，不执行下载的 JS。证明 Cookie 限 www.tiktok.com、当前主播路径、Secure、十秒有效；不进入 fetch/WSS 的 Cookie 白名单。未知挑战直接失败，重复挑战耗尽次数后停止。

真实 `clairecreatesjourney` 房间验证：

- check/register 成功。
- 第一次直播页 1462 字节，确认 WAF、无 SIGI_STATE。
- 同会话第二次页面 247616 字节，无 WAF，有房间数据。
- 匿名 fetch：HTTP 200、protobuf、38813 字节，解析成功。
- 另一轮完整旁观启动：available=true，failure_stage=null，WSS 主机 webcast-ws.tiktok.com；随后关闭。

## 账号 fetch 对照

固定房间 7699484558527892244、页面动态主机 webcast.tiktok.com，使用账号自身保存 Cookie 和 Chrome 145 UA。每次都重新签名，每次 fetch 使用独立 HTTP Session，未复制页面更新的 Cookie。前两次保持令牌相同，第三次重新 report 换新令牌；除签名和令牌外，查询参数及顺序完全一致。

| 账号 | 原令牌第一次 | 同令牌重新签名 | 新令牌重新签名 |
| --- | --- | --- | --- |
| spider_king017 | 200 protobuf，43814 字节 | 200 protobuf，43814 字节 | 200 protobuf，43814 字节 |
| louisduir8x | 200 protobuf，55191 字节 | 200 protobuf，57830 字节 | 200 protobuf，61574 字节 |

六次均解码成功，UA/browser_version 一致性检查通过，第二、三次签名后的 URL 均不同。诊断只保留布尔一致性、状态、长度及耗时，没有记录凭据或签名原文。

## 结论及限制

WAF 页面解析失败已有可复现原因和修复证据。账号 fetch 的间歇 403/200 空响应本轮未复现，根因未确定；不能把这六次成功归因于旁观 WAF 修复，也不能证明新令牌必然有效或旧令牌必然失败。

当前生产 fetch 失败时仅抛出通用 Initial fetch rejected，上层最终分类 initial_fetch_unavailable，原始 HTTP 状态和内容长度没有保留在该错误中，限制了事后排查能力。本轮使用只读诊断探针捕获这些字段，未擅自改变生产请求策略。下一次失败应优先在相同账号/房间/UA 下记录状态、长度、令牌是否变化与请求时序，再做单变量比较，不应混用账号或盲目切换区域主机。

本轮不是消息展示测试，不能用 fetch 成功率代替评论展示成功率。

## 验证记录

- 修复前新增测试出现两个预期失败：WAF 后不能恢复、重复挑战只能请求一次。
- 修复后页面/WAF/启动取消定向测试 37 项通过。
- 完整 unittest discover：648 项通过，213.506 秒；日志 `build/work/observer-waf-full.log`。
- 独立只读审查未发现阻塞问题。
