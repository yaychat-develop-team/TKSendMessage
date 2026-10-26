# TiktokApis WSS 纯 Python 替换 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用适配 TiktokApis 协议的纯 Python 实现替换全部 TikTokLive 监听和心跳，隔离匿名观察与登录账号心跳，保留现有发送行为并交付无需 Node 的 EXE。

**Architecture:** 先完成 frontier 签名的 Python 迁移与可信对照；通过后依次实现本地协议、会话准备和单连接传输，再接入现有监听与心跳接口。HTTP 聊天发送、两级并发和账号状态合并不重写。最后清除旧依赖，完成全套回归和隔离打包。

**Tech Stack:** Python 3.10、现有 PySide6/DrissionPage/curl_cffi/Loguru/PyInstaller、显式声明的 `websocket-client>=1.8,<2`；不引入 Node、JS 解释器或 TikTokLive。

**Spec:** `D:/Myproject/forya/tiktok_desktop_tool/docs/superpowers/specs/2026-10-20-tiktokapis-wss-design.md`

## Global Constraints

- 展示监听 `delivery_observer`：独立匿名会话，不读取、传入或继承本地发送账号的 Cookie、storage、profile 或认证材料。
- 账号心跳 `account_heartbeat`：携带当前发送账号的登录 Cookie 和匹配的 UA；不同账号、不同直播间连接完全隔离。
- HTTP 消息发送继续使用现有登录态、签名和请求流程，本次不迁移到上游的聊天发送接口。
- 产品运行、测试替身和构建配置均不再依赖 TikTokLive；不保留自动回退到旧实现的路径。
- WSS 签名与协议逻辑用 Python 实现，不包含 Node 运行时，不通过 subprocess、内嵌 JS 引擎或调用浏览器 frontierSign 代替 Python 算法迁移。
- 保留最多三个直播间并发、房间内所有已选账号并发、发送次数/间隔、停止收尾、状态合并、逐条展示核对及现有界面布局。
- 账号心跳继续至少预热 30 秒、至少三次目标房间的实际心跳，并确认有效入房响应；保留既有心跳间距和目标一致性要求，不以本地定时器次数伪造证据。
- 账号 WSS 覆盖该任务连续发送和展示确认阶段。发送阶段断开就阻止后续请求，不在后台自动重连后继续发送；不因展示未观察到而补发。
- 匿名允许自行取得临时访客状态，但不得退回使用账号库、ROOM_ID_COOKIE 或登录 profile。账号 Cookie 不能发给第三方签名服务。
- 已完成请求的日志/计数不能因本地保存异常、取消或监听故障丢失；响应继续先脱敏后打印前 150 字。
- 保留 Python 3.10 兼容性，旧 EXE、账号、profile、配置和用户未提交改动。没有自动提交、推送、合并或真实聊天发送授权。
- 纯 Python 签名门槛不通过就停止，不实施后续替换、不打包，不把离线通过宣称为真实平台成功。

## Review Focus

1. SDK 隐含的时间/随机/环境状态未显式建模时，Python 结果不能靠截断、补齐或常量蒙混过关；Task 1 对照向量必须能抓住这些变化。
2. 匿名初始化遇到挑战页或缺字段时不能偷偷借用账号材料；账号初始化出现 Cookie 轮换/身份冲突时也不能和访客材料拼凑；Task 3 验证。
3. 重复 URL 参数、空格编码、超大消息 ID 和非 ASCII 正文不能因转换丢失；Task 2/3/5 验证。
4. 服务器持续推送业务帧时仍要发心跳；断线/取消与 ACK 竞争不能残留线程、虚增心跳或重复聊天 POST；Task 4/5 验证。
5. 开发环境恰好装有旧库、Node 或 JS 引擎不能掩盖发布依赖遗漏；Task 6/7 在阻断这些依赖的环境验证。

## 执行约定与版本依据

- 当前工作目录 `D:/Myproject/forya/tiktok_desktop_tool`；Python `D:/Worktools/conda/envs/spider_vip/python.exe`。所有测试设置 `PYTHONUTF8=1`、`QT_QPA_PLATFORM=offscreen`。
- 现有功能基线为已发布 `dist/TKChatWave.exe`，此前 383 项回归通过；执行开始重新记录 Git 状态、EXE 与正式账号文件 SHA256，不读取或输出账号值。
- 正式账号文件为 `dist/runtime/accounts/tiktok_accounts.json`。任务测试只用合成临时数据；全量测试放在不含正式 runtime 的干净副本中运行。
- 工作区已有大量用户改动；保留当前 checkout，逐任务保存 before 快照和审查 diff，不把整个工作区一起提交。本计划不自动 commit；如之后用户要求提交，只暂存明确范围。
- 上游固定 commit：`b3b2a448800e4ca9e061203b9ba1f53d1d6d3082`。参考 `api/tiktok_web.py`、`builder/auth.py`、`builder/signer.py`、`signing/live_wire.py`、`signing/protobuf.py`、`signing/env/sign.js`、`signing/env/env_core.js` 与 `signing/source/webmssdk*.js`；记录来源及适配差异，不整体引入 API facade。
- 只读探查已完成；尚未运行上游 SDK、生成对照、安装新依赖或编写产品实现。开发对照允许使用固定原 SDK，但不能把它加入默认测试/产品依赖；不向真实平台提交 Cookie 或消息。

## 文件职责与接口合同

新增 `tiktok_live/wss/`：

- `models.py`：`SessionContext`、`ChatEvent`、`LiveResponse`、`PushFrame`、`PreparedConnection`，均为 Python 3.10 可用的数据类；认证字段 `repr=False`。
- `signer.py`：纯 Python frontier 算法的会话适配与输入校验。
- `protocol.py`：有限长度 protobuf/gzip 编解码，不依赖旧库。
- `bootstrap.py`：匿名/账号会话及初始 fetch，返回可连接的数据，不写账号库。
- `transport.py`：单连接、心跳/ACK/接收、取消和资源所有权。
- `tiktok_live/algorithms/frontier.py`：迁移后的 Python 算法，不能借 JS 引擎执行原 SDK。

共享类型字段如下，后续任务不得另起近义字段：

```python
Role = Literal['delivery_observer', 'account_heartbeat']
SessionContext(role: Role, target_username: str, room_id: str, live_id: str,
               user_agent: str, referer: str, cookie_header: str,
               document_cookie: str, ms_token: str, device_id: str,
               metrics: dict[str, object], local_storage: dict[str, str],
               session_storage: dict[str, str])
ChatEvent(message_id: str, user_id: str, unique_id: str, nickname: str,
          text: str, historical: bool = False)
LiveResponse(events: tuple[ChatEvent, ...], cursor: str,
             internal_ext: bytes, need_ack: bool, heartbeat_ms: int)
PushFrame(payload_type: str, log_id: int, room_id: str | None,
          response: LiveResponse | None)
PreparedConnection(session: SessionContext, ws_url: str, cursor: str,
                   initial_events: tuple[ChatEvent, ...])
```

业务消息/用户/房间/live ID 从解码到匹配保持十进制字符串，不经过 float/JS Number；外层 log_id 使用无损 Python int 供 ACK 编码。`PushFrame.room_id` 只来自实际包含房间的协议消息；空字段不能伪造目标匹配。`internal_ext` 保留原始 bytes 供 ACK。包含 Cookie、storage 或签名 URL 的字段及整体对象不得直接记录。

## Task 1: 纯 Python frontier 签名与可信对照门槛

**Files:** Create `tiktok_live/algorithms/frontier.py`, `tiktok_live/wss/__init__.py`, `tiktok_live/wss/models.py`, `tiktok_live/wss/signer.py`, `tests/test_wss_signer.py`, `tests/fixtures/wss/frontier_vectors.json`, `docs/third-party/tiktokapis-wss.md`.

**Interfaces:**

```python
# algorithms/frontier.py
frontier_sign(*, cookie: str, user_agent: str, referer: str,
              metrics: Mapping[str, object], now_ms: int | None = None,
              random_float: Callable[[], float] | None = None) -> str
# wss/signer.py
sign_frontier(session: SessionContext, *,
              should_cancel: Callable[[], bool] | None = None) -> str
```

- [ ] 记录固定上游 commit、入口 `byted_acrawler.frontierSign({'X-MS-PAYLOAD': ''})` 的实际调用链、所有影响输出的环境/状态以及来源说明；使用合成会话，不能复制真实 Cookie 或已消费签名。若遇到未建模 SDK 初始化状态，先把它纳入向量输入及上述接口的 `metrics` 明确键，更新合同说明，不能靠固定输出补偿。
- [ ] 用固定时间/随机序列的原算法采集至少 12 个独立对照向量，覆盖空/合成访客 Cookie、合成登录 Cookie、UA/页面/设备环境变化、Unicode 与两组时间/随机状态。向量保存输入、环境状态、原始期望输出、上游 commit 和采集方法；同一输入重复初始化应可重现。先以差分确认哪些输入确实影响结果，不强求本来无关的字段必须影响输出。
- [ ] 写 `test_matches_reference_vectors`：对每条独立向量断言 Python 输出逐字节等于期望值且长度为 16；写 `test_clock_random_and_initialization_state_are_not_ignored`、`test_concurrent_calls_do_not_share_mutable_sdk_state`、`test_invalid_input_and_cancellation_do_not_return_fake_signature`。测试从 JSON 读原算法的期望值，不调用待测实现生成 expected。
- [ ] Run `python -m unittest tests.test_wss_signer -v`；记录 RED 为缺少实现/输出不一致，不接受测试数据损坏或缺依赖代替 RED。
- [ ] 实现算法及显式 Python 状态。默认用当前时间和独立随机源，固定输入只供可重现验证；实现 `sign_frontier` 的校验、取消和安全错误转换，不输出敏感输入。输入上限为Cookie 64 KiB、UA 2 KiB、referer 4 KiB、metrics序列化64 KiB，计算仅允许有界数据遍历，调用前后检查取消；超限安全失败。不得使用 Node/quickjs/iv8/js2py，也不得把 Python 写的通用 JS 执行器当算法迁移。
- [ ] 重跑以上命令，要求全部通过；审查向量来源及至少三组中间步骤的独立对照，确认不是硬编码、截断或“看起来像”签名。正式与默认测试路径在禁止启动 Node/JS 引擎时仍通过。
- [ ] 保存 focused 输出与 scoped diff。**只有可信对照与迁移都通过才继续 Task 2；否则停止报告具体缺口，保留原程序，不发布替换版。**

## Task 2: 有界协议编解码

**Files:** Create `tiktok_live/wss/protocol.py`, `tests/test_wss_protocol.py`; extend Task 1 的 models 与来源记录。

**Interfaces:** `decode_live_response(raw: bytes) -> LiveResponse`、`decode_push_frame(raw: bytes) -> PushFrame`、`encode_heartbeat(room_id: str) -> bytes`、`encode_enter_room(room_id: str, live_id: str, cursor: str) -> bytes`、`encode_ack(log_id: int, internal_ext: bytes) -> bytes`。

- [ ] 写合成 wire fixture 测试：`test_chat_id_above_2pow53_and_unicode_are_preserved`，断言消息 ID `7698560995402582804` 字符串和正文 `你好 👋` 原样保存；`test_ack_echoes_exact_log_id_and_internal_extension` 断言 ACK payload 与原响应 bytes 一致；覆盖 gzip/plain、入房响应、心跳和未知事件。
- [ ] 写 `test_truncated_varint_oversized_and_compression_bomb_fail_bounded`，规定最大 wire 帧 2 MiB、解压后 8 MiB、varint 最多 10 bytes；超限和非法字段抛安全协议错误，不返回半个有效事件，不记录原 payload。
- [ ] Run `python -m unittest tests.test_wss_protocol -v` 记录 RED。
- [ ] 参照固定上游编解码实现最小消息集合，用 Python 标准库完成界限检查和有界 gzip 解压；未知业务 method 忽略，但外层帧仍校验。不得依赖 TikTokLive 的 protobuf 类，也不把 ACK 的 bytes 先做有损 UTF-8 转换。
- [ ] Run `python -m unittest tests.test_wss_protocol tests.test_wss_signer -v`，要求全部通过，保存协议固定向量及 scoped diff。

## Task 3: 匿名与账号初始化隔离

**Files:** Create `tiktok_live/wss/bootstrap.py`, `tests/test_wss_bootstrap.py`; Modify `tiktok_live/wss/models.py` only if补全已明确字段；复用现有浏览器资源管理，不改登录业务。

**Interfaces:**

```python
prepare_anonymous(username: str, *, should_cancel: Callable[[], bool],
                  timeout_seconds: float = 60) -> PreparedConnection
prepare_account(username: str, *, room_id: str, cookie: str, user_agent: str,
                should_cancel: Callable[[], bool],
                timeout_seconds: float = 60) -> PreparedConnection
build_fetch_url(session: SessionContext, cursor: str = '0') -> str
build_ws_url(session: SessionContext, marker: str) -> str
validate_http_destination(url: str) -> None
validate_ws_destination(url: str) -> None
```

- [ ] 写 `test_anonymous_never_reads_store_profile_or_fallback_cookie`：合成账号材料放入禁止访问的边界，匿名流程只允许读取自己的新临时 profile/访客状态，失败不得调用账号加载或 ROOM_ID_COOKIE fallback；`test_two_account_preparations_keep_cookie_ua_and_device_state_paired` 验证并发隔离。
- [ ] 写 `test_browser_context_closes_on_success_timeout_and_cancel`、`test_missing_ms_token_cursor_or_changed_login_identity_fails_closed`；断言所有拥有的进程/临时 profile 在返回前关闭，未产生聊天 POST，正式/私有账号文件均未写入。
- [ ] 写 URL 固定对照：`test_duplicate_version_code_and_space_encoding_match_upstream`，初始 fetch 保留两个 version_code，WS 空格为 `%20` 而非 `+`，不重编码 `/` 等上游保留字符。写 `test_cross_origin_redirect_and_cookie_destination_are_rejected`，拒绝 HTTP、非443端口、userinfo、`tiktok.com.evil.invalid`、`.tiktok.com` 以外主机及非 webcast WS 路径；每一跳都先校验再发送。
- [ ] Run `python -m unittest tests.test_wss_bootstrap -v` 记录 RED。
- [ ] 使用本任务新建的临时 headless Chromium 上下文获取本会话 HTML、Cookie、UA、storage、device/metrics；匿名从空 profile 启动，账号只注入传入 Cookie，并在任何TikTok导航前设置且核对传入UA，校验关键登录身份未被替换。复用 `browser_auth.launch_available_browser` 和有所有权校验的关闭函数，不复用正式 profile，不通过浏览器调用 frontierSign；不能把默认浏览器UA混入账号会话。取消/异常均清理本次资源。
- [ ] 以捕获的同会话材料构造 SessionContext；初始 fetch 使用显式独立 curl_cffi Session、经过验证的目的地、上述有序 query 及现有纯 Python HTTP `signing.sign_url`，解析 Task 2 响应取 cursor。页面纯解析可在函数内延迟复用 client 的 `extract_room_id` 等纯函数，不能调用有账号 fallback 的 `get_live_room`，也不能新增 client→heartbeat→bootstrap 的顶层循环导入。缺少真实材料就安全失败，不生成伪默认 token。
- [ ] 浏览器和 HTTP 临时资源收尾后调用 Task 1 `sign_frontier` 生成本次 marker，构造 PreparedConnection；初始化历史事件标记 historical，不当作实时结果。cookie_header 和 document_cookie 按其实际来源分别保留，不能拿完整 HttpOnly Cookie 冒充 document.cookie。
- [ ] Run `python -m unittest tests.test_wss_bootstrap tests.test_wss_signer tests.test_wss_protocol -v`，再验证 `python -c "import listen_msg; import tiktok_live.client; import app"` 无循环导入；测试全部使用替身，不真实访问直播间。

## Task 4: 独立 WSS 传输与心跳证据

**Files:** Create `tiktok_live/wss/transport.py`, `tests/test_wss_transport.py`; Modify `tiktok_live/heartbeat_socket.py`; add `tests/test_heartbeat_evidence.py`.

**Interfaces:**

```python
LiveWsConnection(*, prepare: Callable[[], PreparedConnection],
                 on_comment: Callable[[ChatEvent], None],
                 evidence: FrameEvidence | None = None)
# public methods/properties
start(timeout: float = 30) -> bool
close() -> None
available: bool; error: Exception | None; room_id: str | None
connected_at: float | None
# heartbeat_socket.py retains
FrameEvidence.record(message: bytes, direction: str, *, now=None) -> None
FrameEvidence.ready(room, *, elapsed, connected) -> bool
FrameEvidence.ready_to_send(room, *, connected) -> bool
FrameEvidence.snapshot() -> dict
validate_destination(uri: str) -> None  # delegates to the shared WS validator
```

- [ ] 写 `test_busy_stream_and_idle_timeout_both_send_real_heartbeats`：受控 socket/时钟在持续消息及超时两种情况下均按期发送，send 抛错不得增加 evidence；`test_ack_and_heartbeat_writes_are_serialized` 验证并发写顺序与 bytes。
- [ ] 写 `test_empty_recv_disconnect_and_cancel_wake_waiters_and_drain_threads`；空串/空bytes视为关闭，不作为无限忽略的非业务帧。取消与入房响应同时到达时不能虚报 available，close 幂等并等待拥有线程结束，不修改外部线程。
- [ ] 写证据测试：30 秒以前即使有三心跳也不放行；至少三次发送、首末间距至少18秒、目标房间一致且确有有效入房响应才放行；只有本地计时或其他房间响应不能放行。响应若有显式房间字段必须一致；协议响应没有该字段时，仅关联当前连接唯一且确已发送的入房请求，证据标明关联来源，不能捏造服务端room_id，也不能仅按收到任意resp计数放行。
- [ ] Run `python -m unittest tests.test_wss_transport tests.test_heartbeat_evidence -v` 记录 RED。
- [ ] 使用 websocket-client 连接（显式 enable_multithread），单独调度心跳并以锁串行写 ACK/心跳，recv 有限超时。默认心跳10秒；有效服务端 heartbeat_duration 在1000–60000ms内才采用，否则保留10秒。真实发送后才记录 evidence，接收无效帧连续三次转为不可用；限制 wire/解压长度沿用 Task 2。
- [ ] WS 握手验证目的地后才使用当前 PreparedConnection Cookie/UA；禁用不经验证的重定向和隐式环境代理认证注入。有效入房响应后才发布 available。初始历史事件不进入实时评论回调；去重缓存最多4096个ID，淘汰不影响新消息。
- [ ] 保存安全连接日志（无 query/认证内容）并显式继承 task context；账号任务发生断线后不在本连接自动重连。Run `python -m unittest tests.test_wss_transport tests.test_heartbeat_evidence tests.test_wss_bootstrap -v` 全通过后记录审查 diff。

## Task 5: 替换监听与发送心跳，保留业务行为

**Files:** Modify `listen_msg.py`, `tiktok_live/heartbeat.py`, `tiktok_live/heartbeat_socket.py`, `tests/test_message_listener.py`, `tests/test_listener_shutdown.py`, `tests/test_send_heartbeat.py`, `tests/test_message_burst.py`, `tests/test_parallel_logs.py`; extend `tests/test_parallel_runner.py` only for集成回归。

**Interfaces:** 保留 LiveMessageListener 的 `start(connect_timeout)`、`close()`、`available/error/room_id`、`record_comment` 和 `wait_for_comment` 参数及 ObservedComment 字段；保留 `room_heartbeat_session(should_cancel=None)` 与 `room_heartbeat(username, room_id, cookie, user_agent)`。新内部连接工厂使用 Task 4 接口，不再仿造 TikTokLive 的 `_client._ws`。若保留构造注入参数名 `client_factory`，其返回对象就是新连接合同，旧库 event decorator 不再保留。

- [ ] 先迁移测试替身到新模型，保留原有真实行为断言。新增 `test_observer_uses_anonymous_prepare_and_heartbeat_uses_selected_cookie`、`test_realtime_message_ids_match_while_initial_history_cannot_match`、`test_receipt_ids_above_2pow53_are_never_rounded`。
- [ ] 保留并加强 repeated-content/消息ID/发送者/时间/emoji/NFKC、三条“有/无/有”、监听失败零POST、断线阻止后续POST、取消保留已完成消息、保存失败保留日志计数的测试；WSS native线程两类用途在Qt/文件sink归属正确，无上下文日志不被污染。
- [ ] Run `python -m unittest tests.test_message_listener tests.test_listener_shutdown tests.test_send_heartbeat tests.test_message_burst tests.test_parallel_logs -v` 记录具体 RED，不用删除旧断言换绿。
- [ ] LiveMessageListener 对接匿名 prepare 和新传输，统一 ChatEvent→ObservedComment，保留队列中的后续ID供后续匹配；等待中连接失败时及时结束，不让超时被误报展示不存在。控制台监听入口也改用新实现，不留下 TikTokLive 客户端旁路。
- [ ] heartbeat 通过账号 prepare 创建本账号连接，使用明确的 available/room_id/evidence/connected_at；保留60秒预热截止、30秒/三心跳/18秒间距/入房门槛、ContextVar及ExitStack持有连接至消息确认完成。异常和取消只清理自己的资源。
- [ ] Run上述focused命令及 `python -m unittest tests.test_parallel_runner tests.test_parallel_state tests.test_desktop_workers tests.test_saved_request_identity tests.test_response_log_preview -v`；不修改runner POST调度、重试次数或账号合并规则。若必须调整调用边界，仅传递新监听状态，补对应行为测试后 scoped review。

## Task 6: 清除依赖与修正所有发布入口

**Files:** Modify `requirements.txt`, `app.py`, `build/tiktok_desktop.spec`, `build/tiktok_desktop_onefile.spec`, `build/tiktok_desktop_macos.spec`, `build/verify_release.py`, `tests/test_app_startup.py`, `tests/test_release_manifest.py`, `tests/test_macos_build_config.py`, `docs/desktop-usage.md`, `docs/architecture.md`, `README.md`; Create `tiktok_live/wss/self_test.py`, `tests/test_wss_dependencies.py`。Reuse unchanged `build/build.ps1`, `build/build_macos.sh`, `build/smoke_release.py`, `build/smoke_onefile.py`, `build/macos_one_click.py`, `build/package_macos_source.py`：现有规则自动收集Python文件，四个smoke标志不变，不为清单齐全而修改无关脚本。

**Interfaces:** `tiktok_live.wss.self_test.run_offline_self_test() -> bool`，只验证一组空Cookie合成签名对照、合成协议及传输依赖导入；不读账号、不发网络请求。供 app smoke 的 `dependencies_loaded` 使用。生产包中只有该最小无认证自检，不复制整个tests/fixtures或上游SDK；固定期望仅用于比较，绝不传给真实签名流程。

- [ ] 写 `test_import_and_offline_smoke_without_old_or_js_runtimes`：子进程import hook阻断 TikTokLive、TikTokLiveProto、Node启动及JS引擎，清理NODE_PATH/PATH依赖后导入listener/heartbeat/app，运行本地签名向量与合成协议自检；断言无网络/登录/聊天调用且自检成功。
- [ ] 写 `test_release_has_python_signer_and_no_legacy_library_or_node_runner`：先对合成archive/manifest验证缺少frontier模块、含旧库/Node签名运行器时拒绝；最终包另做实际archive核对。不能只搜索源码字符串代替导入和启动验证。历史文档提及旧库或 macOS 应用名 TikTokLiveTool 不应被当成依赖误删。
- [ ] Run `python -m unittest tests.test_wss_dependencies tests.test_app_startup tests.test_release_manifest tests.test_macos_build_config tests.test_macos_one_click -v` 记录 RED。
- [ ] 删除requirements的TikTokLive，加入 `websocket-client>=1.8,<2`，清除旧库metadata/data/hiddenimports并显式排除旧包；未再使用的websockets不再依靠传递依赖。移除启动自检对TikTokLiveClient的引用，改为新Python签名/协议/传输的离线自检，不读取账号、不连接真实平台。
- [ ] 三个spec移除旧库收集并排除旧包；verify_release新增旧库/Node签名运行器禁入清单，tests覆盖真实入口合同。保持既有Python白名单与四个smoke标志，运行macOS配置和源码归档测试确认新Python模块仍被收集。无Node资源下载/收集步骤，不引入上游JS签名脚本。文档解释匿名观察和登录心跳用途、Python门槛、原有3房间/账号并发、次数语义及日志状态。
- [ ] 重跑以上测试，再运行依赖阻断子进程的完整旧库清除验证。macOS仅做配置/脚本检查，不在Windows声称已构建 .app。不改变原有账号保存位置或应用名称。

## Task 7: 最终审查、干净回归与EXE交付

**Files:** Reuse现有spec与验证脚本；生成新的 `build/work/tiktokapis-wss-release-20261020/`（若已存在使用新后缀，不覆盖），其下只含本次source_inputs、verification、candidate、previous及验证记录。

- [ ] 执行最终整体审查，关注上述五项Review Focus与跨任务合同；仅把本计划产品差异交给审查者，不混入此前用户改动。修复被证实的阻断项并重跑覆盖测试，记录原理及证据。
- [ ] 从白名单复制最终产品与测试到干净验证副本；正式runtime不参与。Run `PYTHONUTF8=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -v`，要求0 failures/0 errors。使用计划指定解释器，Windows临时目录清理受限时申请相应执行权限，不把权限错误当产品失败反复等待。
- [ ] Run `python build/verify_release.py <source_inputs> --scan-only` 与 `python build/verify_release.py browser/chromium --scan-only`；核对每个staged产品文件hash与最终源码一致。固定SDK参考/对照文件只作为开发证据，不进入发行白名单。
- [ ] 以 `TIKTOK_BUILD_SOURCE=<本次source_inputs>`、独立PYINSTALLER_CONFIG_DIR，Run `python -m PyInstaller --noconfirm --distpath <本次candidate> --workpath <本次pyinstaller> build/tiktok_desktop_onefile.spec`。不运行会覆盖旧dist或清理用户目录的未验证快捷脚本。
- [ ] Run `python build/smoke_onefile.py <candidate/TKChatWave.exe>`，要求四项启动标志均true；在干净PATH且旧库/Node不可用条件下运行新签名离线自检。检查内置浏览器仍可解析，不触发登录或消息发送。
- [ ] 使用PyInstaller archive readers核对新wss模块、Python frontier、listener/heartbeat/app的编译代码与最终源码一致；包内无旧库、Node、JS签名运行器及runtime/accounts/profiles/logs/reports。不得因为环境里装着旧库就把其依赖收集进包。
- [ ] 备份旧EXE至本次previous，发布至 `dist/TKChatWave.exe` 并核对SHA256；原EXE被占用时另存 `dist/TKChatWave-tiktokapis.exe`，不终止用户进程。正式账号哈希需与执行前一致；若外部合法更新，保留新数据并调查，不回滚账号。
- [ ] 交付EXE绝对路径、hash/大小、最终测试和smoke结果，说明不需要安装Node、匿名监听不使用账号Cookie、账号心跳携带登录态；公开声明未做真实聊天测试，离线对照/启动通过不等于服务器接受或观众必然可见。没有经过用户指定的真实房间验证，不宣称已验证实际连通性。

## 自审与执行交接

- 需求映射：纯Python与可信签名Task 1；协议边界Task 2；两类Cookie/环境隔离Task 3；生命周期/证据Task 4；监听/心跳/业务回归Task 5；旧库清除/依赖/文档/构建入口Task 6；最终审查和打包Task 7。
- 接口链：Task 1模型与签名→Task 2协议/Task 3初始化→Task 4传输→Task 5监听及心跳→Task 6自检/Task 7打包，函数名与类型按合同统一。
- 五类Review Focus均已分配行为测试；没有以“输出16字符”“能import”“源码搜不到旧库”代替真实对照和受控执行。
- 计划尚未执行；Node版本只读探查不构成依赖选择，用户追加要求已删除全部Node运行/打包方案。
- 建议 Native：七项任务接口强相关，先由当前会话完成签名门槛及后续实施，再做一次独立整体审查，避免对同一签名上下文重复重建；也可由用户选择逐任务子代理实施和独立审查。
- 审阅更新后的spec和本计划、确认执行方式后再开始。若选子代理，不同时启动互相改共享文件的实现者；若选Native，按 executing-plans 流程实施。两种方式都不自动发送真实消息。
