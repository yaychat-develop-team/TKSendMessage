# TikTok QR Login and Multi-Account Cookie Sessions Implementation Plan

> 归档说明：这是实现阶段使用的计划记录。当前交付已经拆分为 `tiktok_live/` 包；实际目录和运行方式以根目录 `README.md` 与 `docs/architecture.md` 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add official TikTok QR login, local multi-account Cookie persistence, expiration cleanup, and one-pass message sending to the existing Python live-message script.

**Architecture:** Keep the existing signed HTTP message pipeline intact and add a DrissionPage browser-session layer around it. Browser cookies are stored as versioned browser Cookie objects, validated in an isolated browser context before use, converted to a request Cookie string, and then passed to the existing `send_live_message()` function.

**Tech Stack:** Python 3.10+, DrissionPage, curl_cffi, Loguru, unittest, JSON, pathlib

**Spec:** `D:\Myproject\spider_vip\practice\intermediate_case_study\字节系\tiktoc\tk5.3.0\tk5.3.0\algorithms\send_live_message_design.md`

## Global Constraints

- Modify `send_live_message.py` without replacing the existing signing, room lookup, or message-response interpretation pipeline.
- Use `https://www.tiktok.com/login` and a visible system Chrome window controlled by DrissionPage for login.
- Store all accounts in `tiktok_accounts.json` beside the Python script.
- Never log Cookie values, `msToken`, CSRF data, URL signatures, or Ticket Guard data.
- Log the target username, message, full `roomId`, HTTP status, business status, and final result.
- A script run processes each valid saved account once; it must not send in an infinite loop.
- Network, proxy, WAF, and timeout failures must preserve saved accounts.
- Delete an account only for locally expired critical cookies or a clear server-side logged-out state.
- Do not reuse headers, payloads, or request-specific signature values captured during login; generate them for each message request.
- The target directory is not a Git repository, so commit steps are not applicable.

## Review Focus

- A Cookie file containing malformed JSON must be preserved and produce a clear error; Task 1 tests this.
- Two scans of the same account must update one record rather than create duplicates; Task 1 tests this.
- A temporary network failure during validation must not delete an account; Task 3 tests this.
- A user closing the browser before login must not save incomplete cookies; Task 2 tests this.
- Mutation of the account list during removal must not skip later accounts; Task 4 tests one-pass snapshot iteration.

---

## File Structure

- Modify: `algorithms/send_live_message.py` — existing signing and sending plus Cookie storage, DrissionPage login, validation, and orchestration.
- Modify: `algorithms/send_live_message.md` — installation, configuration, JSON format, login flow, validation behavior, and troubleshooting.
- Create: `algorithms/test_send_live_message.py` — offline unit tests using temporary directories and injected browser/session functions.
- Runtime output: `algorithms/tiktok_accounts.json` — generated locally, contains versioned browser Cookie objects and must not be committed.

### Task 1: Versioned Account Store

**Files:**
- Modify: `algorithms/send_live_message.py`
- Create: `algorithms/test_send_live_message.py`

**Interfaces:**
- Produces: `load_account_store(path: Path) -> dict[str, Any]`
- Produces: `save_account_store_atomic(path: Path, store: dict[str, Any]) -> None`
- Produces: `account_id_from_cookies(cookies: list[dict[str, Any]]) -> str`
- Produces: `upsert_account(store: dict[str, Any], cookies: list[dict[str, Any]], now: datetime) -> dict[str, Any]`
- Produces: `remove_account(store: dict[str, Any], account_id: str) -> bool`
- Produces: `cookie_header_from_playwright(cookies: list[dict[str, Any]]) -> str`

- [ ] **Step 1: Write failing store tests**

Add tests named `test_missing_store_returns_versioned_empty_store`, `test_malformed_store_is_preserved_and_rejected`, `test_upsert_same_account_updates_without_duplicate`, `test_atomic_save_round_trips_cookie_objects`, and `test_cookie_header_keeps_only_tiktok_cookie_name_values`.

- [ ] **Step 2: Run the store tests and confirm RED**

Run: `python -m unittest test_send_live_message.AccountStoreTests -v`

Expected: failures because the account-store interfaces do not exist.

- [ ] **Step 3: Implement the account-store interfaces**

Use schema `{"version": 1, "accounts": []}`. Derive account IDs from a SHA-256 digest of the first available stable identifier among `uid_tt`, `sessionid`, `sessionid_ss`, and `sid_tt`; never log the source value. Save to a sibling temporary file and replace the destination atomically.

- [ ] **Step 4: Run the store tests and confirm GREEN**

Run: `python -m unittest test_send_live_message.AccountStoreTests -v`

Expected: all account-store tests pass with no credentials printed.

### Task 2: Official QR Login Capture

**Files:**
- Modify: `algorithms/send_live_message.py`
- Modify: `algorithms/test_send_live_message.py`

**Interfaces:**
- Consumes: `upsert_account(...)`, `save_account_store_atomic(...)`
- Produces: `has_critical_login_cookie(cookies: list[dict[str, Any]]) -> bool`
- Produces: `wait_for_tiktok_login(page: Any, context: Any, timeout_seconds: int, poll_seconds: float = 1.0) -> list[dict[str, Any]]`
- Produces: `login_new_account(store_path: Path, timeout_seconds: int = 300) -> dict[str, Any]`

- [ ] **Step 1: Write failing login-state tests**

Add tests named `test_login_requires_critical_cookie_and_exit_from_login_flow`, `test_login_requires_two_stable_poll_cycles`, `test_closed_browser_does_not_return_cookies`, and `test_login_timeout_does_not_modify_store`. Use small fake page/context objects; do not launch a browser in unit tests.

- [ ] **Step 2: Run the login tests and confirm RED**

Run: `python -m unittest test_send_live_message.LoginStateTests -v`

Expected: failures because login-state functions do not exist.

- [ ] **Step 3: Implement login detection and DrissionPage launch**

Import DrissionPage lazily inside `login_new_account()`. Launch system Chrome with a dedicated persistent profile, preserve third-party OAuth state, clear only TikTok cookies before adding an account, monitor every tab, and save cookies only after two stable successful polls. If DrissionPage or Chrome is unavailable, raise a safe actionable error.

- [ ] **Step 4: Run the login tests and confirm GREEN**

Run: `python -m unittest test_send_live_message.LoginStateTests -v`

Expected: all login-state tests pass; no browser opens during unit tests.

### Task 3: Cookie Expiration from Send Responses

**Files:**
- Modify: `algorithms/send_live_message.py`
- Modify: `algorithms/test_send_live_message.py`

**Interfaces:**
- Produces: `interpret_chat_response(payload: dict[str, Any]) -> dict[str, Any]`
- Produces: a `loginExpired: true` result only when the send response explicitly asks the user to log in or reports an expired login.

- [x] **Step 1: Write response-classification tests**

Cover JSON and non-JSON login-required responses, plus ordinary network and business failures that must preserve the account.

- [x] **Step 2: Implement response-driven expiration**

Use saved cookies directly for the send request. Do not start a browser or make a separate validation request. Remove an account only when the actual send response explicitly indicates that login is required or expired. Preserve the account for WAF, rate limits, timeouts, network failures, malformed responses, and other business errors.

- [x] **Step 3: Run regression tests and confirm GREEN**

Run: `python -m unittest discover -s tests -q`

Expected: all regression tests pass, including account deletion after explicit login-required responses.

### Task 4: One-Pass Multi-Account Orchestration

**Files:**
- Modify: `algorithms/send_live_message.py`
- Modify: `algorithms/test_send_live_message.py`

**Interfaces:**
- Consumes: account store, optional login, `send_live_message(username, cookie, message)`
- Produces: `run_saved_accounts(username: str, message: str, store_path: Path = ACCOUNT_STORE_PATH, add_new_account: bool = ADD_NEW_ACCOUNT, interval_seconds: float = ACCOUNT_REQUEST_INTERVAL_SECONDS) -> dict[str, int]`

- [ ] **Step 1: Write failing orchestration tests**

Add tests for missing or empty stores, explicit account addition, direct saved-cookie sends, login-required deletion, failure preservation, and per-account intervals.

- [ ] **Step 2: Run orchestration tests and confirm RED**

Run: `python -m unittest test_send_live_message.AccountRunnerTests -v`

Expected: failures because `run_saved_accounts()` does not exist.

- [ ] **Step 3: Implement one-pass orchestration**

Iterate over a snapshot of `accounts`. Call `send_live_message()` once for every saved account without browser validation. Remove only accounts whose send result has `loginExpired: true`, preserve all other failures, and sleep only between accounts. Return counters named `success`, `failed`, `expired`, and `temporaryError`.

- [ ] **Step 4: Replace the direct script entry point**

Set `ACCOUNT_STORE_PATH = CURRENT_DIR / "tiktok_accounts.json"`, add `ADD_NEW_ACCOUNT = False` and `ACCOUNT_REQUEST_INTERVAL_SECONDS = 5`, remove the manual Cookie field from `MANUAL_INPUT`, and call `run_saved_accounts()` from `if __name__ == "__main__"`.

- [ ] **Step 5: Run orchestration tests and confirm GREEN**

Run: `python -m unittest test_send_live_message.AccountRunnerTests -v`

Expected: all orchestration tests pass and each fake account sends once.

### Task 5: Logging, Documentation, and End-to-End Verification

**Files:**
- Modify: `algorithms/send_live_message.py`
- Modify: `algorithms/send_live_message.md`
- Modify: `algorithms/test_send_live_message.py`

**Interfaces:**
- Consumes: all prior task interfaces.
- Produces: documented operator workflow and sanitized diagnostic logs.

- [ ] **Step 1: Add failing log-sanitization tests**

Add `test_account_runner_logs_message_room_and_result_without_credentials`. Capture Loguru output and assert it contains account index, target username, message, `room_id`, HTTP status, and business status; assert it does not contain any Cookie value, `msToken`, CSRF, signature fields, or Ticket Guard data.

- [ ] **Step 2: Run the log test and confirm RED**

Run: `python -m unittest test_send_live_message.LoggingTests -v`

Expected: failure until orchestration logging is complete.

- [ ] **Step 3: Implement sanitized orchestration logs and summary**

Log scan start/result, account short ID, validation classification, send start/result, and final counters. Preserve the existing full `roomId` and message logs. Do not pass raw account dictionaries or exception request objects to Loguru.

- [ ] **Step 4: Update the operator documentation**

Document `pip install DrissionPage loguru curl-cffi`, switch semantics, JSON location and schema, QR flow, one-pass behavior, expiration deletion, temporary-error preservation, log fields, and recovery steps.

- [ ] **Step 5: Run the complete offline suite**

Run: `python -m unittest test_send_live_message -v`

Expected: every test passes with zero failures and no credentials in output.

- [ ] **Step 6: Run syntax and source checks**

Run: `python -c "import ast,pathlib; p=pathlib.Path('send_live_message.py'); ast.parse(p.read_text(encoding='utf-8-sig')); print('syntax ok')"`

Expected: `syntax ok`.

- [ ] **Step 7: Perform supervised browser verification**

Set `ADD_NEW_ACCOUNT = True`, run `python send_live_message.py`, scan one test account, and confirm `tiktok_accounts.json` contains one account without printing its Cookie values. Then restore `ADD_NEW_ACCOUNT = False`.

- [ ] **Step 8: Verify saved-session startup and one message request**

Run `python send_live_message.py` again. Confirm no login window opens, the saved account validates, exactly one message request is made, the full `roomId` is logged, and the final summary reports one processed account. Do not repeat the network send after a successful status `0` response.

