# 多直播间与本地账号并发发送 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 调整发送设置布局，支持分号分隔直播间、最多 3 房间并发及每房间全部已选账号并发，并逐条记录哪个账号在哪个直播间的展示核对结果，重新交付 EXE。

**Architecture:** 在现有单房间 runner 外增加两层协调：房间执行器最多 3 个任务，房间任务内部并发运行所有已选账号；每个账号任务复用原有 runner，但使用独立账号文件和报告目录。独立状态模块准备快照并统一合并账号变化；日志通过任务上下文标明房间和账号，统计按任务最新快照求和。Qt 工作线程等待子任务安全退出，不直接从子线程修改控件。

**Tech Stack:** Python 3.10、concurrent.futures、threading、tempfile、PySide6、Loguru、现有 DrissionPage/TikTokLive/PyInstaller；不新增依赖。

**Spec:** `D:/Myproject/forya/tiktok_desktop_tool/docs/superpowers/specs/2026-10-20-multi-room-send-design.md`

## Global Constraints

- 最多 3 个直播间同时运行，其他排队；一个直播间全部账号结束后才释放房间名额。
- 运行中的每个房间内全部已选本地账号并发，不额外设置隐藏的账号串行队列；单房间同样适用。
- 每账号每房间发送次数 1–100、默认 1，间隔 0.1–3600 秒、默认 1 秒。
- 每个账号每个房间至少 30 秒预热、至少 3 次心跳；发送和展示确认期间保持对应 WSS。
- 不因未观察到展示自动加发。取消阻止后续请求及排队房间启动，保留已完成结果并等待资源收尾。
- 所有真实账号、profile、配置及用户未提交改动保留；不执行未经用户指定的真实发送测试。
- 响应先脱敏再仅打印前 150 字；日志保留 WSS 用途标识和逐条账号/房间/消息序号/消息 ID/展示状态。
- 账号任务不竞争写正式账号文件，报告不互相覆盖，不使用用户输入拼接不受控路径。
- 本计划已完成；最终发布为 `dist/TKChatWave.exe`，383 项回归通过，先前版本已备份。详细验证及审查记录位于本计划的 `.superpowers/sdd/2026-10-20-multi-room-send/`。

## Review Focus

1. 房间名大小写重复、全角分号、空项及非法路径输入必须在网络操作前处理（Task 1）。
2. 同账号在多个房间同时失效或刷新、以及用户在外部更新账号时，不丢记录、不用旧快照覆盖新登录（Task 2）。
3. 3 个房间均运行时停止、单账号抛异常或提交线程失败，必须收尾而非悬挂 Qt 工作线程（Task 3）。
4. 相同消息连续发送多次、响应日志被截断或账号缺用户名时，每条结果仍有准确归属，不能串用展示回执（Task 4）。
5. 窄窗口、长目标列表、部分失败和重复累计进度，布局可用且总统计不重复计数（Task 5）。

---

## Task 1: 目标解析与零发送校验

**Files:** Create `tiktok_live/targets.py`, `tests/test_targets.py`; Modify `tiktok_live/errors.py`.

**Interfaces:** `parse_targets(value: str) -> tuple[str, ...]`；非法输入抛出 `SafeOperationError('invalid_targets')`，安全提示说明使用用户名和分号，不回显整段非法输入。

- [x] 写 table-driven 测试：`' @Alpha ; beta;;alpha；@GAMMA '` 得到 `('Alpha', 'beta', 'GAMMA')`；空串、只有分号、`../x`、URL、含空白项、`.`/`..` 整体失败，且不调用网络边界。
- [x] Run `python -m unittest tests.test_targets -v`，确认缺失解析行为导致失败。
- [x] 实现分隔、去空项、去单个前导 `@`、用户名字符校验、不区分大小写去重并保留首次顺序；拒绝任一非法非空项。
- [x] 同一测试命令全部通过后，检查该模块没有 GUI、账号文件及网络依赖。

## Task 2: 账号任务快照与安全收尾

**Files:** Create `tiktok_live/parallel_state.py`, `tests/test_parallel_state.py`.

**Interfaces:**

`IsolatedAccountRuns(store_path: Path, temporary_root: Path, selected_account_ids: tuple[str, ...] | None)` 为上下文管理器；进入后提供 `accounts: tuple[dict, ...]`、`create_store(room: str, account_id: str) -> Path`、`collect_outcome(account_id: str, task_store: Path, counters: dict) -> None`、`commit() -> None`。实现只使用 account_store 的读写校验与原子保存接口，不让并发任务共用一个文件。

- [x] 测试两个房间的同账号 task store 路径不同、各文件仅含本账号、账号环境种子一致、未选账号不会成为任务。使用临时合成账号，不读取正式 runtime。
- [x] 测试三种合并：任务中的展示用户名刷新可以保存；仅当任务正常返回、`expired > 0` 且该账号确实从任务库移除时收集失效；正式记录被外部改动后，不覆盖或删除外部的新记录。
- [x] 测试冲突优先级：同一启动记录被一个任务刷新、另一个确认失效，且正式记录未改变时，失效移除优先；任务抛错、task store 损坏或读失败不得推断失效。
- [x] Run `python -m unittest tests.test_parallel_state -v`，先红后绿。
- [x] 进入上下文时读取正式库一次、规范化重复账号及确定性环境种子；必要的首次保存发生在任何子任务启动之前。保留未选账号。
- [x] 每个任务使用内部生成的房间/账号哈希目录，保存在本次独立 TemporaryDirectory 中；收集深拷贝状态并用锁保护结果集合。
- [x] `commit()` 在任务全部结束后重读正式库，以启动记录相等性为条件合并：同一账号的已确认失效优先，否则选最后完成的确有变化的有效任务记录；未变化记录不写回。无变化则不重写文件。清理仅限本次临时根，不清理用户 profile、日志或正式库。

## Task 3: 房间与账号两层并发协调

**Files:** Create `tiktok_live/parallel_runner.py`, `tests/test_parallel_runner.py`; Modify `desktop/workers.py`, `tests/test_desktop_workers.py`.

**Interfaces:**

`run_parallel_targets(targets: tuple[str, ...], message: str, *, store_path: Path, temporary_root: Path, report_dir: Path, selected_account_ids: tuple[str, ...] | None, message_count: int, message_interval_seconds: float, should_cancel: Callable[[], bool], on_progress: Callable[[dict], None] | None, runner_kwargs: dict) -> dict`。

结果含原有计数及 `rooms`：每房间的每账号最新快照、完成/失败/取消状态；不含 Cookie、原始账号记录或内部 task store 内容。桌面信号继续发出计数及安全状态。

- [x] 用 threading.Event/Barrier 控制假 runner，测试 4 房间×2 账号：前 3 房间的 6 个账号任务均能进入网络边界，第四房间在释放名额前未启动；任一房间只结束一个账号时仍占用房间名额。
- [x] 测试单房间×2 账号同样重叠运行；每任务拿到不同账号库、单个 selected ID、正确用户名、相同次数与间隔；消息次数不随并发翻倍。
- [x] 测试取消时排队房间不启动，运行任务均收到取消并关闭；一个账号异常不停止其他账号，一个房间失败不停止其他房间；线程提交失败也进入安全收尾。
- [x] 测试累计回调相同值重复发出时总计不增加，更新后只按最新快照求和；回调在锁保护下串行发出，结束结果与累计一致。
- [x] Run `python -m unittest tests.test_parallel_runner -v`，先红后绿。
- [x] 外层 ThreadPoolExecutor 固定 `max_workers=3`；每房间内层执行器 `max_workers=len(selected_accounts)`。每个子线程重新建立房间/账号 Loguru 上下文；不依赖 ContextVar 自动跨线程传播。
- [x] 每账号调用现有 `run_saved_accounts`，传任务私有库、唯一报告目录、单账号过滤及既有 settings.runner_kwargs；不得调用登录流程，不额外重试消息 POST。
- [x] 每个任务异常记录安全 error type/classification，汇总为该任务失败，不传播秘密异常文本；最后等待所有子任务完成，调用快照协调层 commit 并清理临时数据。
- [x] 接入 SendWorker：保留空输入、无账号、未勾选等既有提示，解析全部目标后再启动协调层。`_last_counts` 保存汇总计数，Qt 窗口仍只通过 queued signals 更新。
- [x] 更新 worker 的边界测试到协调层，继续验证所有既有取消/finished/失败安全约束；Run `python -m unittest tests.test_parallel_runner tests.test_desktop_workers -v`。

## Task 4: 逐条展示结果及并发日志归属

**Files:** Modify `tiktok_live/runner.py`, `desktop/log_bridge.py`; Create `tests/test_parallel_logs.py`; Extend `tests/test_message_burst.py`, `tests/test_daily_logs.py`.

**Interfaces:**

`log_message_display(*, room: str, account: dict, index: int, total: int, message_id: str, status: str, classification: str) -> None` 位于 runner；`status` 限 `observed/not_observed/listener_unavailable/send_failed`。固定日志格式包含 `room`、`account_username`（无值时用短 ID）、`account_id`、`message_index=i/N`、`message_id`（缺失用 `-`）、`display_status` 及 classification。消息 ID 仅作为数字元数据，不输出原始响应或 Cookie。

- [x] 先写行为测试：两房间×两账号同时打印相同正文，捕获 Qt/文件 sink 输出，确认每条的房间和账号正确；日志上下文不能污染随后无上下文的普通日志。
- [x] 测试同一账号 3 个响应 ID 的展示结果为“有/无/有”时，恰有 3 条逐条结果，序号和 ID 一一对应；不因完整响应被截断而丢失账号/房间。
- [x] 测试监听不可用、无用户名、请求失败及取消后已有 POST 的结果均有归属；未发出的排队消息不能记录为“发送失败”或“未展示”。
- [x] Run `python -m unittest tests.test_parallel_logs tests.test_message_burst -v`，先红后绿。
- [x] 在既有 runner 的每个已处理消息终态分支调用日志接口，不改变消息 ID 核对和请求次数；展示日志明确区分已观察到与未观察到，不承诺观众一定可见或不可见。
- [x] QtLogSink 与 DailyLogSink 从任务 context 的安全元数据添加统一前缀，再走原有脱敏逻辑；禁止通过修改全局 logger patcher 串改其他线程记录。普通无 context 日志维持兼容。
- [x] Run `python -m unittest tests.test_parallel_logs tests.test_message_burst tests.test_daily_logs tests.test_response_log_preview -v` 全部通过。

## Task 5: 界面布局、端到端回归与 EXE

**Files:** Modify `desktop/main_window.py`, `tests/test_main_window.py`, `docs/desktop-usage.md`; reuse existing build spec and smoke scripts.

**Interfaces:** 保留 `targetUsername`、`messageText`、`messageCount`、`messageIntervalSeconds` 控件名。目标占位符说明 `;` 分隔，次数可访问名称说明“每账号每直播间”；`SendWorker.finished` 的房间结果用于安全完成提示，失败房间/账号不误报全成功。

- [x] 先写 Qt 测试：消息框左右占满内容行，两个参数控件均在其下方且同排对齐；1100×850、800×700 两种尺寸下不重叠、不截断核心标签；忙碌时目标/正文/参数/账号编辑禁用，停止仍可用。
- [x] 测试 `A;B` 传入协调层的两个目标正确；重复房间只调度一次；非法目标不产生任务；多次累计进度与最终房间提示符合实际结果。
- [x] Run `python -m unittest tests.test_main_window tests.test_desktop_workers -v`，先红后绿。
- [x] 将次数、间隔移到消息框下方的独立参数行，左对齐、保留整行消息框及足够行间距；更新操作说明、并发资源占用提示、逐条日志示例和次数的乘法语义。
- [x] 使用受控 Qt 预览检查两种尺寸，不读取或展示真实账号 Cookie。
- [x] 以干净验证副本运行 `PYTHONUTF8=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -v`；必须 0 failures/0 errors，不用真实 runtime 参与发布扫描。
- [x] 创建新的 `build/work/parallel-release-20261020`，只拷贝白名单产品源码；运行 `build/verify_release.py <source_inputs> --scan-only` 及浏览器扫描，确认通过。
- [x] 使用 `TIKTOK_BUILD_SOURCE=<source_inputs>` 和既有 `build/tiktok_desktop_onefile.spec` 构建独立候选 EXE；不覆盖账号/profile。
- [x] 运行 `build/smoke_onefile.py <candidate.exe>`；核对核心模块字节码与最终源码一致，包内无 runtime/accounts/profiles/logs/reports，所需依赖齐全。
- [x] 备份原 EXE，发布并核对 SHA256。若原程序被占用，另存 `dist/TKChatWave-parallel.exe`，不强制终止用户进程。核对正式账号文件哈希未被构建或 smoke 改变。
- [x] 交付 EXE 绝对路径、测试结果、并发与次数语义。说明只做受控并发/启动验证，未额外向真实直播间发送。

## Self-review 与执行门槛

- 需求映射：输入和布局 Task 1/5，3 房间及账号并发 Task 3，账号和报告隔离 Task 2/3，逐条展示日志 Task 4，WSS/次数语义由 Task 3 复用并在 Task 4/5 回归，打包 Task 5。
- 先前“同房间账号顺序发送”的设计已废止，本计划统一为已选账号并发。
- 所有接口的参数名与上下游传递一致；生产实现不携带只供测试使用的方法。
- 测试先覆盖可观察行为，不以搜索源码字符串代替并发、取消或展示结果验证。
- 已按用户批准的子代理分工方式实施和独立审查，最终干净副本回归、构建、包内隐私/源码核对、空账号 EXE 自检均完成；未运行真实发送。
- 保留现有工作区、账号及配置；未提交、推送或合并 Git。
