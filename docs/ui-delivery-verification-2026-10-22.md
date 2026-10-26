# 运行中查看与展示标记：验证记录

## 本次范围

只修改桌面查看/锁定交互、每账号展示状态列及测试，不修改网络发送协议。
状态由现有每房间、每账号的累计进度计算，不从日志或接口 HTTP 成功推断展示。

## 已验证

- 最终源码的主窗口测试：48 项通过。
- 单独运行首次 fetch 顺序与启动超时测试：10 项通过。
- 1100×850 与 800×700 的合成账号截图已检查；标记使用绘制图标，不依赖特殊字体字符。
- 运行中查看与保存锁定、已打开窗口同步锁定和恢复、刷新保留结果、下轮重置、部分成功、取消、跨房间及旧格式 ID 匹配均有回归覆盖。
- 发布输入目录扫描通过，使用独立 staging，不将用户 runtime 目录打入 EXE。
- 新 EXE 包内源码一致性/清单核对通过，包内代码的 204 项指定离线回归通过。
- 新 EXE 在独立空账号目录启动自检通过：窗口、依赖、随包浏览器资源、空账号状态均正常；没有启动浏览器。

## 完整测试限制

首次完整测试共 622 项，9 项断言失败、8 项错误，不能称为全套通过。
该轮开始于最终图标与旧格式 ID 修正前；这些后续改动由上述最终主窗口测试覆盖。
用修改前的 `release-20261022-1757-app` 源码运行相关 51 项测试，复现了同样的 15 项发送/日志失败；它们不是本次 UI 引入。
首次 fetch 和启动超时测试在独立进程复测共 10 项全部通过，完整套件存在顺序/计时相关问题。
分发安全测试扫描的是整个开发工作目录，其中已有用户 runtime、报告及测试日志；实际干净发布输入扫描单独通过。未删除用户数据来使该测试通过。

完整套件中失败或出错的测试名称如下，保留以便后续修理旧测试，不掩盖验证限制：

### tests.test_regression.AccountRequestCookieTests

- test_chat_post_failure_is_not_retried
- test_observation_retry_reuses_room_and_only_reposts_chat_message
- test_pre_send_token_failure_retries_at_most_three_times
- test_send_flow_reuses_saved_login_chrome_profile

这些旧夹具提供的 `object` 不具有新流程要求的 HTTP session `close()`。

### tests.test_regression.LoggingTests

- test_business_response_is_logged_with_credentials_redacted
- test_non_json_error_does_not_expose_response_preview
- test_non_json_login_required_response_is_classified_for_removal
- test_account_runner_logs_message_room_and_result_without_credentials
- test_room_log_uses_saved_sender_name_when_room_lookup_lacks_account_identity
- test_room_log_uses_saved_username_when_saved_nickname_is_missing

前三项旧替身不接受 `known_fetch_endpoint`；后三项预期成功计数与夹具执行结果不符。

### tests.test_room_enter_before_chat.RoomEnterBeforeChatTests

- test_initial_fetch_and_wss_use_the_saved_account_user_agent
- test_initial_fetch_precedes_chat_with_shared_room_and_device
- test_initial_fetch_uses_isolated_http_session_and_page_endpoint
- test_rejected_initial_fetch_stops_before_chat
- test_send_path_never_calls_separate_room_enter

全套运行中命中了前面日志测试使用的合成异常；独立运行全部通过。

### 其他

- tests.test_startup_scope.StartupScopeTests.test_late_resource_is_closed_and_success_disarms_deadline：完整运行超时，隔离复测通过。
- tests.test_distribution_safety.DistributionSafetyTests.test_source_copy_contains_no_runtime_credentials：整个开发目录含既有运行数据；发布采用另建干净 staging 并扫描。

本次未使用真实账号发送评论；验证的是界面和既有事件接线，不是重新验证 TikTok 真实展示能力。

## 高 DPI 后续修正

状态图标原先仅有 16×16 物理像素，顶部图标被预缩至 36×36。
现为状态图标准备 100% 至 400% 的独立绘制分辨率（含 125%、150%、175%、250%），
顶部图标保留 144×144 像素并标记 DPR=4，逻辑尺寸保持 36×36。

最终主窗口测试 50 项通过。独立进程验证结果：

| 缩放 | 状态图标物理像素 | 顶部图标物理像素 | 表格文字裁切 |
| --- | --- | --- | --- |
| 100% | 16 | 36 | 无 |
| 125% | 20 | 45 | 无 |
| 150% | 24 | 54 | 无 |
| 200% | 32 | 72 | 无 |

修改前在 150% 对照中仍返回 16/36 像素，修改后为 24/54，证明不再拉伸低分辨率标记。
Qt 原生 Windows 进程诊断显示 Per-Monitor V2=true、DPR=1.5；默认字体为 Microsoft YaHei UI 9。
离屏测试使用相同字体；另外运行未覆盖字体的原生 Windows 合成账号窗口，检查截图与文字边界通过。
没有更改用户系统缩放、字体设置，也没有关闭 Windows 字体抗锯齿。
尚未做两个物理显示器之间拖动窗口的实机测试，不把离屏多倍率测试当作跨屏实机验收。

高 DPI 版本的包内源码核对、206 项指定离线回归和空账号 EXE 启动自检均通过。
本轮完整测试 625 项：9 项断言失败、7 项错误，失败名称均在上文旧测试列表中；
本轮启动超时用例通过，没有新增 DPI/UI 测试失败。正式 EXE 使用干净 staging 打包，
不包含运行账号和测试目录。
