"""Safe domain errors and operator-facing diagnostics."""

from loguru import logger

_INSTALL_HELP = "请运行 python -m pip install DrissionPage，然后重启。"
_SAFE_ERROR_MESSAGES = {
    'consecutive_not_observed': '同一账号在本直播间连续 3 条消息未观察到展示，已停止该账号在本房间的后续发送；不等同于禁言，不影响其他账号或直播间。',
    'room_comment_restricted': '直播间设置禁止评论，已停止本房间后续发送，不影响其他直播间任务。',
    "room_offline": "接口校验当前房间未开播，未连接 WSS、未发送消息。",
    "room_lookup_transient": "房间查询网络暂时不可用，将按运行设置重试，未发送消息。",
    "room_state_unconfirmed": "无法确认当前房间正在直播，未连接 WSS、未发送消息。请检查网络后重试。",
    "invalid_targets": "请输入直播间用户名；多个用户名请用分号分隔。",
    "listener_unavailable": "直播间监听连接重试已耗尽，本次任务已停止，未发送任何消息。请检查主播用户名、开播状态和网络后重试。",
    "csrf_token_unavailable": "未获取到发送所需的 CSRF token，未发送消息。请检查网络或重新登录后重试。",
    "room_enter_unavailable": "HTTP 进入直播间请求未成功，未发送消息。请检查直播间状态、账号或网络。",
    "initial_fetch_unavailable": "首次直播数据请求未成功，未连接 WSS、未发送消息。请检查直播间状态、账号或网络。",
    "account_store_corrupt": "账户库损坏或格式不兼容。请先备份原文件，再恢复有效的版本 1 备份。",
    "account_store_unreadable": "无法读取账户库。请检查文件权限与文件是否可访问。",
    "account_save_failed": "账户保存失败，未发送当前账户消息。请检查目录写入权限、磁盘空间和文件占用后重试。",
    "drissionpage_missing": "缺少 DrissionPage。" + _INSTALL_HELP,
    "chromium_unavailable": "无法启动系统 Chrome 或 Chromium。" + _INSTALL_HELP,
    "login_timeout": "登录超时，未保存本次登录。请重新运行并在等待时间内完成登录。",
    "browser_close_failed": "登录浏览器未能关闭，未保存本次登录。请手动关闭本次登录窗口后重试。",
    "login_cancelled": "登录窗口已关闭或登录已取消，未保存本次登录。请重新运行并保持登录窗口打开。",
    "run_cancelled": "运行已手动取消。",
    "login_state_unavailable": "连续多次无法读取登录状态，未保存本次登录。请检查登录窗口和网络后重试。",
    "login_page_unavailable": "无法打开官方登录页面，未保存本次登录。请检查网络后重试。",
    "login_identity_unavailable": "登录 Cookie 尚未形成可识别的账户状态，未保存本次登录。请重新登录并等待 TikTok 正式页面完全打开。",
    "login_cookie_incomplete": "登录 Cookie 缺少 s_v_web_id，未保存本次登录。请保持登录页面打开并等待 TikTok 完成安全环境初始化。",
    "ticket_guard_unavailable": "当前账号缺少有效的 Ticket Guard 会话，未发送消息。请重新登录账号后重试。",
    "runtime_error": "运行未完成。请检查配置、依赖和本地文件后重试。",
}

class _SafeOperationError(Exception):
    def __init__(self, classification: str):
        self.classification = (classification if classification in _SAFE_ERROR_MESSAGES
                               else "runtime_error")
        super().__init__(_SAFE_ERROR_MESSAGES[self.classification])


class _AccountStoreError(_SafeOperationError, ValueError):
    pass


class RetryableRoomLookupError(_SafeOperationError):
    """Only a transport timeout/temporary failure, never room-state evidence."""
    def __init__(self):
        super().__init__('room_lookup_transient')


class _AccountFileError(_SafeOperationError, OSError):
    pass


class _BrowserError(_SafeOperationError, RuntimeError):
    pass


class _LoginTimeoutError(_SafeOperationError, TimeoutError):
    pass


def _log_safe_error(classification: str) -> None:
    classification = (classification if classification in _SAFE_ERROR_MESSAGES else "runtime_error")
    logger.error("运行错误 | classification={} | {}", classification,
                 _SAFE_ERROR_MESSAGES[classification])

SafeOperationError = _SafeOperationError


def safe_failure_reasons(values):
    """Bounded diagnostic codes only; never forward raw server/exception text."""
    allowed = set(_SAFE_ERROR_MESSAGES) | {
        'send_exception', 'send_failed', 'expired', 'account_silenced', 'account_banned',
        'accepted_not_observed', 'observation_required', 'observation_disabled',
    }
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value if isinstance(value, str) and value in allowed
                             else 'runtime_error' for value in values[:32]))

AccountStoreError = _AccountStoreError
AccountFileError = _AccountFileError
BrowserError = _BrowserError
LoginTimeoutError = _LoginTimeoutError
log_safe_error = _log_safe_error

