"""Plain, credential-free per-room delivery diagnostics."""
from tiktok_live.errors import safe_failure_reasons

UNCERTAIN = {'listener_unavailable', 'accepted_not_observed',
             'observation_disabled', 'observation_required', 'consecutive_not_observed'}
STAGES = {
    'consecutive_not_observed': '连续 3 条消息未观察到展示，该账号在本房间已停发；不等同于禁言，不影响其他账号和直播间',
    'room_comment_restricted': '直播间设置禁止评论，本房间已停止发送，不影响其他直播间任务',
    'initial_fetch_unavailable': '首次直播数据 fetch',
    'room_enter_unavailable': '进入直播间 enter',
    'csrf_token_unavailable': '发送前 CSRF 准备',
    'ticket_guard_unavailable': '账号签名会话准备',
    'room_offline': '直播状态检查',
    'room_lookup_transient': '房间信息查询',
    'room_state_unconfirmed': '直播状态检查',
    'listener_unavailable': '旁观监听 / 展示确认（无法确认是否展示）',
    'accepted_not_observed': '展示确认（接口接受，未观察到）',
    'observation_disabled': '展示确认（未启用监听）',
    'observation_required': '展示确认（需要监听确认）',
    'send_failed': '消息发送', 'send_exception': '消息发送流程',
    'expired': '账号登录态', 'account_silenced': '账号在本直播间被禁言，已停止后续发送',
    'account_banned': '服务端明确提示账号封禁，已停止后续发送',
    'run_cancelled': '主动停止', 'runtime_error': '未提供可定位的具体阶段',
}
STATUSES = {'queued': '等待执行', 'running': '执行中', 'completed': '已结束',
            'failed': '失败', 'cancelled': '已停止'}


def reasons_for(account):
    reasons = safe_failure_reasons(account.get('failure_reasons'))
    error = account.get('error')
    if isinstance(error, dict):
        reasons += safe_failure_reasons([error.get('classification')])
    return list(dict.fromkeys(reasons))


def format_room_details(room, account):
    codes = reasons_for(account)
    if not codes:
        if account.get('listenerUnavailable'):
            codes.append('listener_unavailable')
        if account.get('acceptedNotObserved'):
            codes.append('accepted_not_observed')
        if account.get('expired'):
            codes.append('expired')
        if account.get('failed') and not codes:
            codes.append('send_failed')
    lines = [f"直播间：{room}", f"任务状态：{STATUSES.get(account.get('status'), '未确认')}",
        f"已发送消息条数：{account.get('sent', 0)}（重试不重复计数）",
        f"已展示：{account.get('delivered', 0)}；已接受/未观察到：{account.get('acceptedNotObserved', 0)}；监听不可用：{account.get('listenerUnavailable', 0)}",
        f"流程失败计数：{account.get('failed', 0)}；登录失效：{account.get('expired', 0)}；临时错误：{account.get('temporaryError', 0)}"]
    lines.extend(f"阶段：{STAGES.get(code, '其他流程')}\n错误分类：{code}" for code in codes)
    if not codes:
        lines.append('错误分类：无具体错误记录；未确认不等于不可见。')
    return '\n'.join(lines)
