# TikTok Live Desktop Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows desktop EXE that accepts only a target TikTok username and message, embeds the DrissionPage Chromium login window inside the app, and runs the existing saved-account send flow with live status and sanitized logs.

**Architecture:** Create an isolated copy at `D:\Myproject\forya\tiktok_desktop_tool`, retain the existing `tiktok_live` package as the business core, and add a PySide6 desktop layer. Login and sending run in worker threads; a Windows-only controller embeds Chromium by PID into a Qt host widget. PyInstaller produces an `onedir` distribution containing a fixed Chromium build while all writable account/profile/report data stays in an external `runtime` directory.

**Tech Stack:** Python 3.10, PySide6 6.x, DrissionPage 4.1.1.2, ctypes Win32 APIs, Loguru, unittest, PyInstaller 6.x.

**Spec:** `docs/superpowers/specs/2026-10-14-tiktok-desktop-tool-design.md`

## Global Constraints

- Windows desktop only; the Chromium login surface must remain inside the tool.
- Copy source from `D:\Myproject\forya\tiktok`; never copy accounts, cookies, profiles, reports, caches, or logs.
- The original `D:\Myproject\forya\tiktok` project remains unchanged after the initial source copy.
- The main UI exposes only target username and message as required inputs.
- Preserve the current signing, room lookup, account deduplication, listener, and result-classification behavior.
- `MESSAGE_NOT_OBSERVED_RETRIES = 3` means three total chat POST attempts, including the first request.
- Never display or package full Cookie, `msToken`, CSRF, Ticket Guard private data, or browser profiles.
- The release format is PyInstaller `onedir` and includes Chromium.

## Review Focus

- A packaged build starts on a machine without installed Chrome and selects the bundled `chrome.exe`.
- Google/Facebook OAuth opens another Chromium top-level window and the active window is re-embedded instead of escaping the app.
- Closing the embedded browser is treated as login cancellation and leaves no incomplete account entry or orphan Chromium process.
- An empty/missing account store routes the user into embedded login without freezing the UI.
- Release scanning fails the build when account cookies, profile directories, reports, or credential-like values are present.

---

### Task 1: Create the isolated, sanitized project copy

**Files:**
- Create: `D:\Myproject\forya\tiktok_desktop_tool\` from the existing source tree
- Create: `tests/test_distribution_safety.py`
- Modify: `.gitignore`
- Modify: `settings.py`

**Interfaces:**
- Consumes: source files under `D:\Myproject\forya\tiktok`.
- Produces: a self-contained source tree with empty runtime state and `ROOM_ID_COOKIE = ""`.

- [ ] **Step 1: Write `test_source_copy_contains_no_runtime_credentials`**
  Assert that account/profile/report/cache files are absent and `settings.ROOM_ID_COOKIE == ""`.
- [ ] **Step 2: Run the test and verify it fails against an unfiltered copy.**
  Run: `python -m unittest tests.test_distribution_safety -v`.
- [ ] **Step 3: Copy only source, tests, and documentation.**
  Exclude `tiktok_accounts.json`, `.browser-profile`, `port`, `__pycache__`, `failed_accounts.json`, `silenced_accounts.json`, logs, and build output; replace the fallback Cookie with an empty value.
- [ ] **Step 4: Extend `.gitignore` for `runtime/`, `browser/chromium/`, `dist/`, and `build/work/`.**
- [ ] **Step 5: Run the distribution-safety and inherited structure tests.**
  Expected: all pass.

### Task 2: Centralize packaged and writable paths

**Files:**
- Create: `desktop/__init__.py`
- Create: `desktop/paths.py`
- Create: `tests/test_desktop_paths.py`
- Modify: `tiktok_live/config.py`
- Modify: `tiktok_live/browser_auth.py`

**Interfaces:**
- Produces: `RuntimePaths.from_application(app_dir: Path | None = None) -> RuntimePaths` with `app_dir`, `runtime_dir`, `account_store`, `profile_root`, `report_dir`, `log_dir`, and `browser_executable`.
- Produces: `resolve_bundled_browser(app_dir: Path) -> Path | None`.

- [ ] **Step 1: Write path tests for source mode, frozen mode, missing runtime folders, and bundled-browser precedence.**
- [ ] **Step 2: Run tests and verify imports/functions are missing.**
- [ ] **Step 3: Implement immutable `RuntimePaths` and directory creation without importing Qt.**
- [ ] **Step 4: Make copied business configuration accept explicit account/profile/report paths while retaining CLI defaults.**
- [ ] **Step 5: Set `TIKTOK_BROWSER_EXECUTABLE` to the bundled browser only when it exists.**
- [ ] **Step 6: Run path, browser-auth, and account-store tests.**

### Task 3: Add callback-aware, cancellable login lifecycle

**Files:**
- Modify: `tiktok_live/browser_auth.py`
- Create: `tests/test_desktop_login_lifecycle.py`

**Interfaces:**
- Produces: `login_new_account(store_path: Path, timeout_seconds: int = 300, *, on_browser_ready: Callable[[int], None] | None = None, cancel_event: threading.Event | None = None) -> dict[str, Any]`.
- `on_browser_ready` receives `browser.process_id` after launch and before navigation.
- Cancellation raises the existing safe `BrowserError("login_cancelled")` classification.

- [ ] **Step 1: Write tests proving the callback receives the process ID, defaults preserve CLI behavior, and cancellation never saves Cookie data.**
- [ ] **Step 2: Add a test for browser closure during login and for missing `s_v_web_id`.**
- [ ] **Step 3: Run tests and verify the new keyword arguments are unsupported.**
- [ ] **Step 4: Add optional callback/cancellation checks to the existing login loop without duplicating login logic.**
- [ ] **Step 5: Run inherited login polling and new lifecycle tests.**

### Task 4: Implement Windows Chromium window embedding

**Files:**
- Create: `desktop/browser_embed.py`
- Create: `tests/test_browser_embed.py`

**Interfaces:**
- Produces: `Win32Api` protocol/wrapper for window enumeration, process-tree lookup, style changes, `SetParent`, `MoveWindow`, and restoration.
- Produces: `BrowserEmbedController.attach(process_id: int, host_hwnd: int) -> bool`, `refresh() -> bool`, `resize(width: int, height: int) -> None`, and `detach() -> None`.

- [ ] **Step 1: Test selection of visible Chromium top-level windows belonging to the root PID or descendants.**
- [ ] **Step 2: Test attach removes top-level frame styles, reparents the window, and stores original parent/style.**
- [ ] **Step 3: Test `refresh()` switches to a newly active OAuth window in the same process tree.**
- [ ] **Step 4: Test detach restores the original window and is idempotent when the browser already exited.**
- [ ] **Step 5: Implement the ctypes Win32 adapter and the Qt-independent controller.**
- [ ] **Step 6: Run browser-embed tests on Windows.**

### Task 5: Bridge business work and logs into Qt threads

**Files:**
- Create: `desktop/workers.py`
- Create: `desktop/log_bridge.py`
- Create: `tests/test_desktop_workers.py`

**Interfaces:**
- Produces: `LoginWorker(store_path, timeout_seconds, cancel_event)` signals `browser_ready(int)`, `account_saved(dict)`, `failed(str)`, and `finished()`.
- Produces: `SendWorker(username, message, paths, cancel_event)` signals `progress(dict)`, `failed(str)`, and `finished(dict)`.
- Produces: `QtLogSink` emitting only pre-sanitized text.

- [ ] **Step 1: Test login-worker signal order for success, duplicate account, cancellation, and timeout.**
- [ ] **Step 2: Test send-worker validation, empty-store login request, result mapping, and cancellation between account/retry boundaries.**
- [ ] **Step 3: Test the log sink rejects or redacts Cookie, `msToken`, CSRF, and Ticket Guard key patterns.**
- [ ] **Step 4: Implement workers as `QObject` instances moved to `QThread`; do not subclass `QThread`.**
- [ ] **Step 5: Add a cooperative cancellation callback to the copied runner while preserving CLI defaults.**
- [ ] **Step 6: Run worker, runner, retry, and log-safety tests.**

### Task 6: Build the desktop window

**Files:**
- Create: `desktop/main_window.py`
- Create: `desktop/account_view.py`
- Create: `tests/test_main_window.py`

**Interfaces:**
- Produces: `MainWindow(paths: RuntimePaths)`.
- Consumes: workers, `BrowserEmbedController`, sanitized account-store summaries.

- [ ] **Step 1: Test that username and message are the only required editable inputs.**
- [ ] **Step 2: Test start is disabled for blank inputs, double starts are blocked, and controls recover after failure.**
- [ ] **Step 3: Test account rows expose username/short ID/status but no Cookie fields.**
- [ ] **Step 4: Test embedded-browser panel opens on `browser_ready`, resizes, refreshes OAuth windows, and collapses after login finishes.**
- [ ] **Step 5: Test result cards map processed, delivered, accepted-not-observed, failed, expired, silenced, and listener-unavailable counts.**
- [ ] **Step 6: Implement the main window using widgets and layouts only; keep business calls out of the UI class.**
- [ ] **Step 7: Run Qt tests with `QT_QPA_PLATFORM=offscreen`.**

### Task 7: Add the application entrypoint and shutdown coordination

**Files:**
- Create: `app.py`
- Create: `tests/test_app_startup.py`

**Interfaces:**
- Produces: `main(argv: Sequence[str] | None = None) -> int`.
- Consumes: `RuntimePaths`, `MainWindow`, bundled-browser resolution.

- [ ] **Step 1: Test startup creates runtime folders and uses the bundled browser path.**
- [ ] **Step 2: Test a second start while a worker is active is rejected by the window state.**
- [ ] **Step 3: Test app close requests cancellation, detaches the embedded window, and waits a bounded time for workers.**
- [ ] **Step 4: Implement `QApplication` setup, icon loading, exception logging, and main-window launch.**
- [ ] **Step 5: Run startup and full desktop tests.**

### Task 8: Package Chromium and build the onedir release

**Files:**
- Create: `requirements-desktop.txt`
- Create: `requirements-build.txt`
- Create: `build/tiktok_desktop.spec`
- Create: `build/build.ps1`
- Create: `build/verify_release.py`
- Create: `tests/test_release_manifest.py`

**Interfaces:**
- `build.ps1 -ChromiumPath <directory>` produces `dist/TikTokLiveTool/TikTokLiveTool.exe`.
- `verify_release.py <dist-dir>` exits nonzero on missing Chromium/runtime dependencies or credential-bearing files.

- [ ] **Step 1: Test manifest generation and rejection of account/profile/report/cache files and credential patterns.**
- [ ] **Step 2: Declare PySide6 runtime and PyInstaller build dependencies with compatible version ranges.**
- [ ] **Step 3: Write the spec with Qt plugins, DrissionPage resources, TikTokLive dependencies, application files, and `browser/chromium`.**
- [ ] **Step 4: Write `build.ps1` to validate `chrome.exe`, copy Chromium, run tests, invoke PyInstaller, and run release verification.**
- [ ] **Step 5: Build the release and verify startup on the development machine with installed Chrome path unset.**
- [ ] **Step 6: Run the sensitive-data scanner on the completed distribution.**

### Task 9: Document operation and perform final regression

**Files:**
- Create: `README.md` desktop usage section or update copied `README.md`
- Create: `docs/desktop-usage.md`
- Modify: `docs/usage.md`

**Interfaces:**
- Produces: end-user instructions for adding an account, sending, stopping, interpreting results, and moving the onedir package.

- [ ] **Step 1: Document that the entire release directory must remain together and that only username/message are required.**
- [ ] **Step 2: Document embedded login, account-data location, stop semantics, total-attempt semantics, and result classifications.**
- [ ] **Step 3: Run all inherited and desktop tests.**
- [ ] **Step 4: Manually verify embedded login, OAuth-window re-embedding, account save, one send run, cancellation, and clean shutdown.**
- [ ] **Step 5: Verify the original `D:\Myproject\forya\tiktok` tree was not modified during desktop implementation.**
