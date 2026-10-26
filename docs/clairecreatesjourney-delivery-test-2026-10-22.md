# clairecreatesjourney 全账号发送验证

用户批准全部保存账号各发送一条 `Hello, enjoying the stream`。
使用当前包含旁观 WAF 修复的源码，执行正式 runner 发送/心跳/旁观匹配流程，不是旧 EXE。
测试保护：每账号最多一次聊天 POST，关闭未观察到后的补发；每条观察窗口 30 秒；账号库及失败清单不持久化修改。独立匿名旁观匹配内容、发送者和发送时间。

| 账号 | fetch | 聊天接口 | 独立旁观结果 |
| --- | --- | --- | --- |
| boaluodyblala1 | 200，有效 protobuf | HTTP 200，status_code=0 | observed / delivered |
| spider_king017 | 200，有效 protobuf | HTTP 200，status_code=0 | observed / delivered |
| djkfypujiwr | 200，有效 protobuf | HTTP 200，status_code=0 | observed / delivered |
| uhovwkpq102 | 200，有效 protobuf | HTTP 200，status_code=0 | accepted_not_observed |
| louisduir8x | 200，有效 protobuf | HTTP 200，status_code=0 | accepted_not_observed |

共处理 5 个账号，真实聊天 POST 共 5 次，每账号 1 次；接口成功 5/5，独立旁观确认展示 3/5（60%），接口接受但未观察到 2/5。listenerUnavailable=0，expired=0，silenced=0，cancelled=false。

未观察到不等于确定没有展示，也不能仅凭此认定账号禁言或平台过滤。此为单轮结果，不代表长期成功率。没有追加发送，没有打包或覆盖 EXE。

测试脚本首次启动在首条输出前停滞，已停止；之后通过已验证的 Python 启动方式运行的唯一有效进程完成上述五次 POST。已完成的发送未重跑。
