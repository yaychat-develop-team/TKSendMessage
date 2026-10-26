# 历史回归失败排查

## 根因与修复

| 原有失败 | 根因 | 修复 |
| --- | --- | --- |
| 4 项 AccountRequestCookieTests | 测试用 `object()` 充当 HTTP Session，没有 `close()`；仍预期只创建一个 Session | 用可关闭的离线 Session 替身；验证发送与首次 fetch 的两个 Session、UA 一致、fetch 结束关闭 |
| 3 项 LoggingTests 抛参数异常 | 旧兼容入口缺少 `known_fetch_endpoint`、`known_region`、`known_device_id` 参数；补齐后暴露测试缺少登录 Cookie | 补齐入口和下游参数转发；测试使用合成 sessionid，仍检查日志/异常不含凭证 |
| 3 项 LoggingTests 成功数为零 | 同一兼容入口问题，以及 Session 夹具不完整，在被测日志响应前提前失败 | 修复真实入口并补齐夹具，保留成功计数、日志内容与脱敏断言 |
| 5 项 RoomEnterBeforeChatTests | 旧入口把临时依赖永久写到公共 client/runner 模块，前面测试的异常替身污染后面测试 | 每次兼容调用建立独立函数依赖环境，不再改公共模块；涵盖异常、成功、并发与后续调用 |
| 1 项 DistributionSafetyTests | 把整个开发目录当作发布包，误报合法的本地账号、运行日志和历史构建输出 | 保留 Git 跟踪输入检查，运行实际 staging 脚本后扫描独立发布副本；检查源码存在并扫描私密内容，不删除本地数据 |

不是全部“测试写旧了”：兼容入口漏参和依赖污染是产品源码中的真实问题。桌面 workers 使用模块化 runner，不经过这个旧脚本入口；本次没有改桌面发送算法、签名或网络请求。

## 防回归

- `tests/test_legacy_entrypoint.py`：新增上下文转发、异常后无污染、辅助函数无污染、runner 无污染、双线程交错期间和结束后公共依赖不变。
- 隔离环境从内存函数代码创建，不依赖源码文件路径，保留同模块辅助函数和延迟重试闭包的依赖环境。
- `tests/test_regression.py`：修正旧 Session 与登录态夹具，未降低日志脱敏、重试次数和请求顺序断言。
- `tests/test_distribution_safety.py`：使用实际 staging 和扫描器；创建必要构建父目录，支持首次运行。
- 最终全量复跑还发现 `test_close_retains_owner_until_pending_preparation_exits` 偶发失败：10 毫秒不足以保证后台线程已开始。改为按 `entered` 事件取消启动，建立“准备仍在进行”的测试前提，保留 close 必须等待线程退出的断言，补充失败时清理。
- 全部测试使用合成数据；未登录或发送真实评论，未改账号与设置，未重新打包或覆盖 EXE。

## 验证记录

- 原有失败复现：`build/work/legacy-failures-red.log`。
- 新隔离回归先失败：`build/work/legacy-entrypoint-red.log`；并发失败也已独立复现。
- 最终针对性测试 96 项通过：`build/work/legacy-isolated-tests.log`。
- 复跑 637 项时原有 16 项全部消除，仅上述线程调度测试偶发失败：`build/work/legacy-full-final.log`。
- 修复事件同步后的最终全量 **637 项通过，0 failures、0 errors**，耗时 208.979 秒：`build/work/legacy-full-verified.log`。
- 最终相关回归 101 项通过：`build/work/legacy-isolated-final.log`。
- 线程退出测试连续重复 20 次全部通过：`build/work/legacy-thread-repeat.log`。
