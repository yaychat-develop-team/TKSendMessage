# TikTok 登录与多账号 Cookie 会话设计

日期：2026-10-09

> **历史归档：以下正文保留 2026-10-09 的设计记录，不作为当前操作或字段规范。** 其中五秒间隔、浏览器 Cookie 数组持久化、camelCase 时间字段及依赖清单等描述已经过时。当前默认账号间隔为 `ACCOUNT_REQUEST_INTERVAL_SECONDS = 15`；版本 1 账号库落盘使用单个 `cookie` 字符串、`created_at` / `updated_at` 和 `environment_seed`，内存仍兼容 Cookie 数组。`MESSAGE_NOT_OBSERVED_RETRIES = 3` 是包含首次的三次消息 POST 上限；只有已连接监听且需要展示确认的响应未被观察到时重发。CLI 配置以 [usage.md](usage.md) 为准，桌面路径、取消和监听身份限制以 [desktop-usage.md](desktop-usage.md) 为准，当前模块与数据边界见 [architecture.md](architecture.md)。

## 以下为历史设计正文

## 目标

在模块化的 `tiktok_live/` 包中实现基于 DrissionPage 的浏览器登录和本地多账号 Cookie 管理，根目录 `send_live_message.py` 仅保留兼容入口。业务人员只需要运行 Python 脚本并在需要时使用 TikTok 官方登录页完成登录。程序负责保存会话、验证会话、清理失效会话，并按账号逐一调用直播消息发送流程。

本功能不逆向或绕过 TikTok 登录流程，不读取账号密码，也不尝试解除服务端对 Cookie、设备、IP 或浏览器环境的关联。所有登录操作均在 TikTok 官方网页中完成。

## 配置接口

集中配置位于根目录 `settings.py`：

```python
ADD_NEW_ACCOUNT = False
ACCOUNT_REQUEST_INTERVAL_SECONDS = 5

MANUAL_INPUT = {
    "username": "主播用户名，不填写@",
    "message": "要发送的消息",
}
```

自动登录启用后，日常运行不再要求在源码中填写 Cookie。Cookie 文件固定为脚本同目录下的 `tiktok_accounts.json`。

- `ADD_NEW_ACCOUNT = True`：无论当前是否已有账号，都打开官方登录页添加一个账号，本轮只使用刚登录账号发送。
- `ADD_NEW_ACCOUNT = False`：使用 Cookie 文件中的全部现有账号逐个发送。
- Cookie 文件不存在或没有有效账号：自动打开官方登录页。
- 新增账号模式只处理刚登录账号；已有账号模式逐个处理有效账号，账号之间按配置等待。每个账号只记录一个最终结果；发送结果需要弹幕确认但未观察到时，可按配置对该账号重试。

## 本地数据结构

`tiktok_accounts.json` 使用版本化结构：

```json
{
  "version": 1,
  "accounts": [
    {
      "id": "由稳定账号 Cookie 计算的不可逆摘要",
      "label": "account-1",
      "createdAt": "2026-10-09T12:00:00+08:00",
      "updatedAt": "2026-10-09T12:00:00+08:00",
      "cookies": [
        {
          "name": "cookie-name",
          "value": "cookie-value",
          "domain": ".tiktok.com",
          "path": "/",
          "expires": 1790000000,
          "httpOnly": true,
          "secure": true,
          "sameSite": "Lax"
        }
      ]
    }
  ]
}
```

Cookie 以 浏览器 Cookie 对象保存，以保留 domain、path、expiry、HttpOnly、Secure 和 SameSite 信息。日志中不输出 Cookie 内容、摘要输入、token、CSRF 或签名。

写文件使用临时文件加原子替换，避免程序中断后留下半个 JSON。解析失败时保留原文件并报告错误，不自动覆盖损坏数据。

## 官方登录流程

1. 使用 DrissionPage 启动系统 Chrome。
2. 使用 Chrome 原生 UA、中文语言和独立持久化用户目录；保留 Google/Facebook 会话。
3. 打开 `https://www.tiktok.com/login`。
4. 用户在官方页面扫描二维码并完成登录。
5. 程序轮询全部标签页和浏览器级 Cookie，以覆盖 Google/Facebook OAuth 弹窗。
6. 同时满足以下条件时视为登录完成：
   - 存在关键登录 Cookie，例如 `sessionid`、`sessionid_ss` 或 `sid_tt`。
   - 页面已离开登录流程，或登录页不再显示登录界面。
7. 登录状态连续稳定两个检测周期后读取完整 Cookie。
8. 根据稳定会话标识生成不可逆账号 ID；同一账号再次登录时更新原记录，不重复添加。
9. 原子写入 `tiktok_accounts.json` 后关闭浏览器。

用户主动关闭浏览器、超过登录等待时间或 TikTok 返回挑战页时，脚本报告原因，不写入不完整 Cookie。

## Cookie 使用与失效处理

保存 Cookie 后不再启动浏览器进行预校验：

1. 从账户库读取保存的 Cookie 并直接调用直播消息流程。
2. 消息接口明确返回“请登录”“登录后再试”或等价英文提示时，将该账号视为失效。
3. 从 JSON 删除失效账号，原子保存后继续下一个账号。
4. 网络超时、代理错误、WAF、限流和普通业务失败都保留 Cookie，避免误删账号。

只有发送接口明确要求重新登录时才删除记录。

## 消息发送流程

1. 读取并验证 `tiktok_accounts.json`。
2. 根据 `ADD_NEW_ACCOUNT` 和当前账号数量决定是否进入官方登录。
3. 对 Cookie 文件中的账号建立不可变快照，避免循环过程中删除元素导致跳过账号。
4. 将每个账号保存的 Cookie 转换为 `name=value` Cookie 字符串。
6. 调用现有 `send_live_message(username, cookie, message)`。
7. 记录账号序号、目标主播、消息、`roomId`、HTTP 状态、业务状态和最终结果。
8. 每个账号处理结束后等待 `ACCOUNT_REQUEST_INTERVAL_SECONDS`。
9. 完成一轮后输出成功、失败、失效和暂时无法验证的数量。

单次脚本启动只遍历一轮账号，不无限循环发送。

## 错误分类

| 类型 | 行为 |
| --- | --- |
| Cookie 文件不存在或为空 | 自动进入官方登录 |
| JSON 格式损坏 | 报错并保留原文件，不覆盖 |
| 服务端明确未登录 | 删除该账号并保存 |
| 网络超时、代理失败 | 保留账号，标记暂时失败 |
| WAF 或 429 | 保留账号，停止对该账号快速重试 |
| 直播间不存在 | 保留账号，仅记录目标直播间错误 |
| 消息接口业务失败 | 保留账号，输出业务状态和原因 |
| 接口成功但直播间未显示 | 提示降低频率、等待并检查网络 |

## 日志与隐私

Loguru 可以记录：

- 登录窗口启动、登录成功、超时或取消。
- 账号序号和不可逆短 ID。
- Cookie 验证结果，但不包含 Cookie 内容。
- 目标主播、发送消息和完整 `roomId`。
- HTTP 状态码、业务状态码和结果说明。
- 本轮成功、失败、失效和暂时失败统计。

Loguru 不记录：

- Cookie 名值明细。
- `msToken`、CSRF、会话 token。
- URL 签名和 Ticket Guard 数据。
- JSON 文件中的原始凭据。

## 依赖

```powershell
pip install DrissionPage loguru curl-cffi

```

如果 DrissionPage 或系统 Chrome 不可用，程序给出明确安装命令，不进入发送流程。

## 测试方案

离线测试覆盖：

- Cookie JSON 新建、读取、更新和原子保存。
- 同一账号再次登录后更新而不是重复添加。
- 过期 Cookie 删除。
- 网络错误不会误删 Cookie。
- Cookie 对象到请求字符串的转换。
- `ADD_NEW_ACCOUNT` 的分支行为。
- 多账号只遍历一轮且保持账号间隔。
- 日志包含消息和 `roomId`，不包含敏感字段。

浏览器验证覆盖：

- 打开 TikTok 官方登录页。
- 用户通过二维码、手机号、邮箱、Google 或 Facebook 等官方方式登录后识别关键 Cookie。
- 登录成功后生成 JSON 文件。
- 第二次运行在 Cookie 有效时不弹出登录页。
- 登录失效时删除对应账号并给出提示。

网络发送验证最多使用一个测试账号发送一条消息，避免测试过程触发频率限制。

