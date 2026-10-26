"""TikTok 直播消息脚本配置。

日常使用只需要修改“必须配置区域”；其余配置已有默认值，通常保持不动。
"""

# ==================== 必须配置区域 ====================

# True：打开登录页面新增或更新一个账号，本轮只使用刚登录的账号发送。
# False：不打开登录页面，循环使用账户文件中已经保存的所有账号。
ADD_NEW_ACCOUNT = False

# username：目标主播用户名，不需要填写 @。
# message：每个账号本轮要发送一次的消息内容。
MANUAL_INPUT = {'username': 'stef_gamingg', 'message': '[sagemoney][sagemoney][sagemoney]'}

# ==================== 可不动区域 ====================
# 以下参数已有可用默认值，只有需要调整超时、频率或监听行为时再修改。

# 等待用户在官方页面完成登录的最长时间，单位为秒。
LOGIN_TIMEOUT_SECONDS = 300
# 多个已保存账号之间的发送间隔，单位为秒
ACCOUNT_REQUEST_INTERVAL_SECONDS = 15

# True：连接直播弹幕流，用于确认接口成功后的消息是否真正公开展示。
# False：完全关闭弹幕监听和 delivered/acceptedNotObserved 展示确认。
MESSAGE_OBSERVATION_ENABLED = True

# True：在控制台打印“[弹幕] 用户名 -> 内容”。
# False：隐藏弹幕输出，但后台监听和消息展示确认仍然运行。
SHOW_LIVE_COMMENTS_IN_CONSOLE = False
# 消息接口返回成功后，等待对应弹幕出现的最长时间，单位为秒。
MESSAGE_OBSERVATION_TIMEOUT_SECONDS = 10

# 未监听到对应弹幕时，单个账号调用发消息接口的总次数（包含首次发送）。
# 默认只发送 1 次，不自动重发；需要重试可在工具中调整总尝试次数。
MESSAGE_NOT_OBSERVED_RETRIES = 1

# 同一账号未监听到消息后的相邻两次发送之间等待时间，单位为秒。
MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS = 2

# 每一次启动弹幕监听时，等待连接成功的最长时间，单位为秒。
MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS = 15

# 首次监听连接失败后继续重试的次数；3 表示最多共连接 4 次。
MESSAGE_OBSERVER_CONNECT_RETRIES = 3

# 相邻两次弹幕监听连接尝试之间的等待时间，单位为秒。
MESSAGE_OBSERVER_RETRY_DELAY_SECONDS = 1

# 仅在当前发送账号无法从直播页取得 roomId 时使用的兜底 Cookie（不需要配置）。
# 它不会替代发送账号自己的 Cookie，也不会为消息 POST 提供登录身份。
ROOM_ID_COOKIE = ""
