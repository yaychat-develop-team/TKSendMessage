# 独立 Python WSS 接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以独立脚本替换 TikTokLive，分别使用登录 Cookie 维持账号心跳、访客 Cookie 核对展示，验证后交付一个 EXE 并清理已确认的旧版本。

**Architecture:** 提取独立脚本的协议和命名兼容签名分支，使用逐连接会话及不读写全局 CookieJar 的握手传输。通过现有监听接口接入账号心跳和展示确认，不重写 HTTP 发送、界面或两级并发。最终以可执行测试、包内核对和独立启动检查作为发布与删除旧包的门槛。

**Tech Stack:** Python 3.10、websocket-client==1.7.0、现有 PySide6/DrissionPage/curl_cffi/Loguru/PyInstaller；无 Node、JS 引擎、签名子进程、TikTokLive/TikTokLiveProto。

**Spec:** `D:/Myproject/forya/tiktok_desktop_tool/docs/superpowers/specs/2026-10-20-standalone-wss-integration-design.md`，用户已确认。

## Global Constraints

- `delivery_observer` 使用本连接公开页面请求下发的临时访客 Cookie；`account_heartbeat` 使用当前发送账号传入的登录 Cookie，配套同账号 UA。
- HTTP 消息发送、登录浏览器修复、账号选择、界面布局、三个直播间并发及其余排队、房间内已选账号并发、发送次数和间隔均保留。
- 消息 POST 重试策略不是本次改造内容，不把未知发送结果自动重发。
- 原独立脚本保持不变。SHA256：`3700B651DB8245B9F153582B60E83F68807DF35506B6F2FBD9848244351A05B2`。
- 使用脚本中的 `ws_direct` 最小兼容分支；本次不继续之前阻塞的全量 SDK 初始化迁移。
- 禁止靠临时清空全局 jar、全局 monkeypatch 或把整个收包循环串行加锁来解决污染。
- 30 秒预热、至少三次目标房间实际发送成功的心跳、既有心跳间距及有效服务端入房响应门槛不降低。
- 不沿用 CLI 的 600 秒、1000 条聊天、10000 帧或固定总发送预算作为任务自动结束条件。
- 删除产品运行、默认测试和构建入口对 TikTokLive/TikTokLiveProto 的依赖，不保留旧实现自动回退。
- 延续不联网、不读取真实账号的验证边界；不自动进行真实登录、平台连接或弹幕发送。
- 新包未验证或任何必要验收失败时，不删除现有工具 EXE。不得递归按 `*.exe` 删除。

## Review Focus

1. 同主机并发握手及服务器 Set-Cookie 不能把账号 A 的 Cookie 混入账号 B 或访客连接；Task 3 的真实本机对端验收。
2. HTTP 101 头与首个二进制帧同一 TCP 包到达时，握手不能吞掉首帧；Task 3 测试。
3. 高频弹幕、接收超时、超过 CLI 原运行预算都不能让心跳饿死或提前关闭；Task 4 测试。
4. 历史消息、相同正文不同发送者/ID、大整数 ID 和停止时的已完成 POST 不能产生假展示或丢结果；Task 1/5 测试。
5. 候选损坏、目标路径为 reparse point 或旧 EXE 被占用时，不能误删旧包、Chromium 或 runtime；Task 7 测试。

## 执行环境与工作区

工作目录为 `D:/Myproject/forya/tiktok_desktop_tool`，解释器为 `D:/Worktools/conda/envs/spider_vip/python.exe`，下文简称 `$py`。测试设置 `PYTHONUTF8=1`、`QT_QPA_PLATFORM=offscreen`，TEMP/TMP 指向项目的 `build/work/standalone-wss-temp`，不依赖沙箱默认临时目录。

现有工作区包含未提交改动。执行前记录状态与受影响源码快照；先按 using-git-worktrees 检查现有 checkout/附件，不使用会遗漏这些改动的干净默认分支替代当前源码。不得 reset、覆盖用户文件、提交全部工作区或自动推送。逐任务检查 diff；本计划沿用不自动提交的约定。

已有 Facebook 登录修复的最近验证是 403 项通过，最新包为 `dist/TKChatWave-facebook-login-fix-v2.exe`。这是历史基线，不代替新版本验证。禁止读取或哈希真实账号文件；账号完整性只在合成临时 fixture 中验证。真实 runtime 不属于测试或扫描输入。

现有未完成的 `wss/models.py`、`wss/signer.py`、`algorithms/frontier*.py` 和相关测试不覆盖、不删除、不接入新运行链路。新文件名显式区分 direct 方案。准备、测试和构建均不得执行开发 SDK 或原脚本的 main。

## 文件及共享接口

新建：`tiktok_live/wss/direct_models.py`、`direct_signer.py`、`protocol.py`、`sessions.py`、`isolated_socket.py`、`connection.py`。分别负责数据合同、签名/URL、字节协议、两类会话准备、独立握手/帧 I/O、单连接生命周期。

修改：`listen_msg.py`、`tiktok_live/heartbeat_socket.py`、`tiktok_live/heartbeat.py`、依赖声明、app 自检、构建配置及相应测试。runner/parallel_runner 的业务行为不变；只在必要的公开接口调用点作兼容接入。

共享数据合同均为 Python 3.10 dataclass：

```python
DirectSession(role: Literal['delivery_observer', 'account_heartbeat'],
              target_username: str, room_id: str, user_agent: str,
              cookie_header: str, proxy_url: str | None)
ChatEvent(message_id: str, user_id: str, unique_id: str, nickname: str,
          text: str, room_id: str = '')
DecodedFrame(payload_type: str, log_id: int, need_ack: bool,
             internal_ext: str, events: tuple[ChatEvent, ...],
             room_id: str | None = None)
HandshakeResult(status: int, headers: dict[str, str], subprotocol: str | None)
```

Session 的 Cookie、UA、proxy 字段及握手 headers 均 `repr=False`。消息/用户/房间 ID 保持字符串，log ID 用无损 int。`DecodedFrame.room_id` 仅在实际协议含该字段时填写；事件的空 room_id 可由已校验连接上下文补齐，不宣称这是服务端返回的房间证据。

---

### Task 1: 提取纯 Python 协议与命名签名分支

**Files:** Create `tiktok_live/wss/direct_models.py`, `direct_signer.py`, `protocol.py`, `tests/test_direct_protocol.py`, `tests/fixtures/wss/direct_vectors.json`; Modify `docs/third-party/tiktokapis-wss.md`。

**Interfaces:**

- `direct_signer.frontier_sign(payload: str, *, context: dict) -> str` 保留原脚本的纯函数合同。
- `direct_signer.build_direct_url(room_id: str, *, user_agent: str, random_values: tuple[float, float, float] | None = None) -> str`，命名 profile 为 `standalone_audience_direct_v1`。
- `protocol.decode_push_frame(raw: bytes, *, max_decoded_bytes: int = 4*1024*1024) -> DecodedFrame`；`encode_heartbeat(room_id: int) -> bytes`；`encode_enter_room(room_id: int, live_id: int = 12, cursor: str = '') -> bytes`；`encode_ack(log_id: int, internal_ext: str) -> bytes`。

- [ ] 写 `test_direct_signature_matches_source_vectors`：从只读原脚本的纯函数取得固定随机输入的独立期望值并保存 fixture；生产测试不能用新算法计算自己的 expected。写 `test_duplicate_query_keys_and_raw_alphabet_are_preserved`：两个 version_code 原序保留，签名字母中的 `+`、`/` 不被重新编码。
- [ ] 写 `test_protocol_large_ids_gzip_and_ack_roundtrip`：合成聊天 ID `18446744073709551614` 保持原字符串，ACK 的 log ID/internal_ext 等于服务端输入；写截断、uint64 越界、2 MiB 帧、4 MiB 解压、20000 字段及 UTF-8 异常测试。
- [ ] 运行 `$py -m unittest tests.test_direct_protocol -v`，确认缺少实现或错误字节导致失败，而非导入其他开发 SDK。
- [ ] 提取原脚本对应纯函数，保持原文件哈希不变；UA 同时用于 URL browser_version 和握手。空 cursor 仅用于上述 direct profile，不假称从 HTTP fetch 取得。记录 profile 的已知范围，不调用旧未完成 signer。
- [ ] 重跑本任务测试至 PASS，检查生产函数导入不读取账号、不发 HTTP、不执行 JS；核对来源哈希和适配记录后保存任务 diff。

### Task 2: 建立独立访客和账号会话

**Files:** Create `tiktok_live/wss/sessions.py`, `tests/test_direct_sessions.py`。

**Interfaces:** Consumes Task 1 `DirectSession`。Produces：

```python
prepare_observer(username: str, *, proxy_setting: str = 'system',
                 opener_factory=None) -> DirectSession
prepare_account(username: str, room_id: str, cookie_header: str,
                user_agent: str, *, proxy_setting: str = 'system') -> DirectSession
validate_destination(url: str, expected_room_id: str) -> None
```

`opener_factory(jar, proxy_url) -> opener` 是离线 HTTP 边界；不是任意目标配置入口。

- [ ] 写 `test_observer_uses_only_fresh_visitor_jar`：访客只允许 `ttwid`、`tt_chain_token`、`tt_csrf_token`，保留 secure/domain/path/expiry 约束，遇到任何登录 Cookie 拒绝；两个实例各自 CookieJar，无账号 fallback。
- [ ] 写 `test_blocked_or_changed_owner_page_does_not_borrow_account_state`：WAF、离线、缺 SIGI_STATE、不同 owner、重定向都失败；默认不使用原脚本硬编码房间。写 `test_account_preparation_makes_no_public_http_request`：账号角色保留传入 Cookie/UA，不调用访客准备。
- [ ] 写目标校验表：合法 direct URL 通过，恶意子域后缀、userinfo、非 443、重复且冲突的 room_id、错误路径/协议、CRLF Cookie/UA、缺关键登录 Cookie 拒绝。
- [ ] 运行 `$py -m unittest tests.test_direct_sessions -v` 并观察 RED；按原脚本访客初始化/代理规则实现会话层，仅用公开房间元数据，不传入账号库路径。
- [ ] 重跑至 PASS；HTTP 和 WSS 均不自动跟随重定向，不更改代理配置；检查异常和 repr 不含 fixture 会话值，保存任务 diff。

### Task 3: 实现不经过全局 CookieJar 的实际握手和帧传输

**Files:** Create `tiktok_live/wss/isolated_socket.py`, `tests/test_isolated_wss_socket.py`, `tests/support/local_wss_peer.py`; Modify `docs/third-party/tiktokapis-wss.md`。

**Interfaces:** Consumes Tasks 1/2。Produces：

```python
perform_handshake(sock, url: str, *, cookie_header: str,
                  user_agent: str, timeout: float) -> HandshakeResult
open_socket(session: DirectSession, url: str, *, timeout: float) -> IsolatedWebSocket
# IsolatedWebSocket: send(payload: bytes, opcode: int = 2), recv_frame(),
# close(timeout: float = 3), connected: bool; frame fields opcode/data/fin.
```

- [ ] 用本机 socket 对端写 `test_concurrent_roles_and_set_cookie_never_cross_sessions`：A、B、访客三连接带不同合成 Cookie，对端返回 Set-Cookie；预先污染库全局 jar 后每个实际握手头仍只有自己的 Cookie，且全局 jar 不变。
- [ ] 写 `test_header_and_first_frame_in_one_write_preserve_frame`、`test_bad_accept_redirect_and_oversize_headers_never_connect`：对端同一次 send 发完整 101+首帧，首帧完整保留；错误 Accept、302、417、超过 32 KiB 响应头都不能设 connected=True。响应错误不读取任意长度 body。
- [ ] 运行 `$py -m unittest tests.test_isolated_wss_socket -v` 观察 RED。测试在最低层 stream 连接边界注入本机已连接 socket；仍校验生产 URL 的 TikTok host，不能增加关闭域名/TLS 校验的产品开关。
- [ ] 实现自有有界握手：随机 Sec-WebSocket-Key、精确 HTTP 101 和 Upgrade/Connection/Accept 校验，读取不越过响应头。继承 websocket-client 1.7.0 帧能力并覆盖 connect 的默认共享握手路径；复用其 HTTP CONNECT/TLS stream 建连能力，TLS 保持证书及主机名校验。拒绝重定向，Set-Cookie 不写入全局状态。
- [ ] 保留 2 MiB frame_buffer 限制；不启用库 trace，不把 Cookie、原始 URL/响应头放入异常。必要复用代码保留 Apache-2.0 等对应许可说明。
- [ ] 重跑本机对端测试至 PASS，同时验证双向分片、ping/pong/close 和提前断开；如沙箱阻断回环，使用禁止外网/真实 runtime 访问的已审查保护脚本，仅放行本机通信。保存任务 diff。

### Task 4: 建立任务拥有的连接、心跳和协议证据

**Files:** Create `tiktok_live/wss/connection.py`, `tests/test_direct_connection.py`; Modify `tiktok_live/heartbeat_socket.py`。

**Interfaces:** Consumes Tasks 1–3。Produces：

```python
DirectConnection(session: DirectSession, *, on_comment, on_connected,
                 connect_timeout: float = 15.0,
                 socket_factory=open_socket, clock=time.monotonic)
# run(stop_event: threading.Event) -> None; close() -> None
# connected: bool; room_id: str; evidence: FrameEvidence
FrameEvidence.record(message: bytes, direction: str, *, now: float | None = None) -> None
FrameEvidence.ready(room, *, elapsed: float, connected: bool) -> bool
FrameEvidence.ready_to_send(room, *, connected: bool) -> bool
FrameEvidence.snapshot() -> dict
```

`on_comment(ChatEvent) -> None` 和 `on_connected() -> None` 在拥有该连接的线程调用；run 在失败时抛安全异常，finally 断开并设 connected=False。

- [ ] 写 `test_busy_idle_and_timed_out_receives_keep_heartbeats`、`test_exceeding_cli_limits_does_not_stop_owned_connection`：控制时钟/流量，超过 600 秒、1000 评论和 10000 帧后仍受任务 stop_event 控制，10 秒心跳不被忙流或超时饿死。
- [ ] 写 `test_only_successful_target_room_sends_and_server_entry_count`：失败 send 不加计数；错误房间、不足三心跳、跨度小于既有 18 秒、缺服务端入房响应均不 ready；30 秒门槛在业务 gate 再验证。
- [ ] 写取消与 ACK 竞争、畸形帧、heartbeat failure、close 后重复 close 和初始化取消测试；断开唤醒拥有者，不启动重连，不泄漏线程/socket。
- [ ] 运行 `$py -m unittest tests.test_direct_connection -v` 观察 RED；实现单所有者收帧循环，启动超时来自 connect_timeout，1 秒接收检查窗口，分片缓冲/去重各有界，按脚本 ACK 原值，证据在成功 send 后更新。
- [ ] FrameEvidence 改用本地协议，不使用 TikTokLive.proto；保留统计键 `sent`、`received`、`entered_rooms`、`heartbeat_gaps_s`、`parse_errors`，每房间心跳时间样本最多 10 个，去重最多 4096 ID。
- [ ] 重跑至 PASS，检查持续忙流、close、取消均不让连接跨任务续用，保存任务 diff。

### Task 5: 接入现有监听和账号发送 gate

**Files:** Modify `listen_msg.py`, `tiktok_live/heartbeat.py`, `tests/test_message_listener.py`, `tests/test_listener_shutdown.py`, `tests/test_send_heartbeat.py`, `tests/test_message_burst.py`, `tests/test_parallel_logs.py`; Test existing `test_parallel_runner.py`, `test_main_window.py`, `test_login_cookie_context.py`。

**Interfaces:** Consumes Task 2 factories and Task 4 connection。`LiveMessageListener(unique_id: str, *, show_comments: bool = True, session_factory=None, connection_factory=DirectConnection, connection_role: str = 'delivery_observer')`；session_factory 为无参数返回 DirectSession 的 callable。保留 start(connect_timeout)、close、available、error、room_id、record_comment、wait_for_comment；新增只读 `connection: DirectConnection | None` 供心跳 gate 读取。

- [ ] 迁移测试替身到新 connection 合同，写 `test_observer_default_never_receives_account_cookie` 与 `test_account_gate_prepares_exact_account_room_session`；不继续模拟 TikTokLive 的 ConnectEvent、proto 或 `_client._ws`。
- [ ] 写 `test_old_history_and_same_text_other_sender_do_not_match`、`test_large_message_id_keeps_exact_match`、`test_queue_overflow_is_bounded_and_nonblocking`：4096 评论缓存满时丢最旧项，不阻塞网络心跳；wait 的 deferred 回填也不能越界。
- [ ] 保留并重跑 gate 的 30 秒、三次心跳、18 秒跨度、入房响应、错误房间/断线阻止 POST 测试。连续三条消息期间账号 socket 只建一次，观察结束才关闭；取消不多发，不为未发送项写终态。
- [ ] 运行 `$py -m unittest tests.test_message_listener tests.test_listener_shutdown tests.test_send_heartbeat tests.test_message_burst tests.test_parallel_logs -v` 观察 RED；将监听线程改为会话准备→DirectConnection.run，线程继承 contextvars，不把线程启动当连接成功。start(connect_timeout) 将剩余启动预算传入 DirectConnection；初始化超过预算时不发布 available，并取消/关闭该任务资源。
- [ ] heartbeat 的默认 session_factory 调用 prepare_account；observer 调用 prepare_observer；保留 ContextVar/ExitStack 生命周期，公开 connection/evidence 替代旧私有对象。不改 HTTP POST 或消息尝试次数逻辑。
- [ ] 重跑上述及现有并发、GUI、登录回归至 PASS；验证账号/房间/role 日志及原有终态完整，保存任务 diff。

### Task 6: 清除旧依赖并验证冻结入口

**Files:** Modify `requirements.txt`, `app.py`, `build/tiktok_desktop.spec`, `build/tiktok_desktop_onefile.spec`, `build/tiktok_desktop_macos.spec`, `build/diagnose_delivery.py`, `build/verify_release.py`, `tests/test_app_startup.py`, `tests/test_macos_build_config.py`, `tests/test_release_manifest.py`; Create `tests/test_direct_dependencies.py`; Modify `README.md`。

**Interfaces:** `tiktok_live.wss.direct_signer.self_test() -> bool` 使用固定可信向量；app 的 `--smoke-test` 保留四个 JSON 检查键 `window_visible`、`bundled_browser`、`empty_accounts`、`dependencies_loaded`，最后一项改为新协议、签名向量及 websocket 能力自检。

- [ ] 写 `test_import_and_smoke_work_with_old_libraries_denied`：在干净子进程以 import hook 拒绝 TikTokLive、TikTokLiveProto、JS 引擎后导入新链路并运行自检；不卸载开发机包，不读真实 runtime。
- [ ] 写构建声明与冻结 archive inventory 测试，断言新入口可运行、旧库/Node/开发 SDK 不进入候选；历史文档或 macOS 的 `TikTokLiveTool.app` 产品名不误判为运行时依赖。
- [ ] 运行 `$py -m unittest tests.test_direct_dependencies tests.test_app_startup tests.test_macos_build_config tests.test_release_manifest -v` 观察 RED。
- [ ] 从依赖删除 TikTokLive，声明 `websocket-client==1.7.0`；三个 spec 去除旧库 data/metadata/hidden import 并显式排除旧库，收集 websocket 必要包。更新 diagnose_delivery 使用公开 connection/evidence；默认测试不能再导入旧 proto。
- [ ] app 和发布清单验证 Task 1 self_test，不启用旧未完成 SDK signer；自检不产生 HTTP/WSS 业务请求。README 标明 direct profile 范围和未进行真实平台验收。
- [ ] 在不含 runtime/browser profile 的白名单源码副本运行 `$py -X faulthandler -m unittest discover -v`，全部 PASS 才进入发布；失败按名称记录，不用局部通过代替完整回归。保存任务 diff。

### Task 7: 验证、发布并安全清理旧 EXE

**Files:** Create `build/publish_verified_exe.py`, `tests/test_publish_verified_exe.py`; Modify `build/build.ps1`, `build/smoke_release.py`, `build/verify_release.py`（仅必要的 onefile 支持）。

**Interfaces:** `publish_verified_exe(candidate: Path, project_root: Path, *, expected_sha256: str, checks: dict[str, bool]) -> dict`。只处理 project_root/dist 顶层白名单，返回 published/removed/remaining 信息；checks 必须含且通过 `unit_suite`、`source_scan`、`loopback_protocol`、`frozen_smoke`、`archive_inventory`。

- [ ] 在合成临时项目写 `test_unverified_corrupt_or_reparse_candidate_deletes_nothing`、`test_locked_destination_keeps_candidate_and_old_versions`、`test_publish_removes_only_known_top_level_app_versions`：runtime/Chromium/未知 EXE 字节不变，候选哈希错误或任一 check=False 不动旧包；替换失败不清理变体。
- [ ] 运行 `$py -m unittest tests.test_publish_verified_exe -v` 观察 RED；实现仅经校验路径的候选发布：重新核对哈希与非 reparse 路径，先成功发布 `dist/TKChatWave.exe`，再删除三个已确认旧文件名。不能关闭/杀死被占用程序；失败报告剩余项并要求用户关闭。
- [ ] 再次枚举原脚本哈希和 dist 顶层 EXE 清单，保留当前旧版本直到全部 gate 成功。白名单构建输入写入新的 build/work 子目录，排除 bytecode/runtime/原 CLI 设置；源码及 Chromium 隐私扫描 PASS。设置 PYTHONDONTWRITEBYTECODE=1 防止构建扫描输入被缓存污染。
- [ ] 使用 `build/tiktok_desktop_onefile.spec` 构建到 `build/work/standalone-wss-candidate`；构建退出码必须为 0。构建路径与删除目标都先解析校验，不借跨 shell 拼接删除命令。
- [ ] 在空账号临时目录运行候选 `--smoke-test`，四个检查均为 true 且未创建账号文件；检查 archive 中新实现与已验证源码一致，确认无旧库、Node、开发 SDK、账号数据。启动不得指向正式 dist/runtime。
- [ ] 用候选包内的握手/协议代码连接受控本机对端，重新证明登录/访客合成 Cookie 隔离、101+首帧、心跳/ACK。源码测试通过但冻结入口失败时停止，不发布或删旧包。
- [ ] 所有 check 与候选 SHA256 记录到发布报告后调用 publish_verified_exe。核对仅剩新 `dist/TKChatWave.exe`（如有未知 EXE 则保留并列明），不要递归处理 browser、build/work 或其他项目。
- [ ] 交付新路径、哈希、测试数量、真实平台未实测范围及实际删除清单；永久删除的旧文件明确告知不能从回收站恢复。未得到额外测试授权时，不读取登录账号做线上验收。

## 自审与执行交接

协议/签名→Task 1，会话隔离/目的地→Task 2，全局缓存/握手→Task 3，心跳/生命周期/有界资源→Task 4，展示/并发/日志/登录回归→Task 5，依赖/全部入口→Task 6，构建与删除→Task 7。五项 Review Focus 均有对应可执行测试；共享类型和消费接口在相邻任务中同名。

本计划替代旧 WSS 全量 SDK 迁移计划的后续执行路径，不改写其历史记录或删除未提交代码。设计已获用户确认；本文仍需用户审阅。延续此前“当前会话直接实施”的偏好，确认计划后使用 executing-plans，实施结束再做独立只读复核；本阶段未开始产品实现。
