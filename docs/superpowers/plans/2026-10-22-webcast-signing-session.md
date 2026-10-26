# Stateful Webcast Signing Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make account-bound enter and initial fetch use one browser-free, per-page signing session whose identity, browser version, SDK state, and token sources are validated rather than copied from one capture.

**Architecture:** Python owns account-isolated HTTP, cookies, ordered requests, and WSS bootstrap. A small signing-session object passes explicit page state into existing Python signature primitives. If one field cannot be reproduced in Python, a deterministic local JS helper may calculate only that field after fixed-input parity; it never drives a browser or performs HTTP.

**Tech Stack:** Python 3.10+, `curl_cffi`, existing signature/protobuf code, `unittest`; optional local JavaScript runtime only for a proven opaque field.

**Spec:** `docs/superpowers/specs/2026-10-21-webcast-initial-fetch-design.md`

## Global Constraints

- No third-party signing service or browser automation in the final send path.
- Never save or log raw Cookie, token, signature, request URL, or protobuf body; use synthetic fixtures and redacted metadata.
- A signed User-Agent, `browser_version`, Client Hints major, and selected transport fingerprint must agree; unsupported majors fail explicitly.
- One account/page owns one HTTP Cookie jar and signing state. Anonymous observers and parallel accounts remain isolated.
- Sign each actual request freshly. Do not copy Chrome's observed `21/50` counters, SDK `1.0.0.2937`, canvas, or query/Cookie token into defaults.
- When encoding request query or form parameter sequences with `urlencode`, pass `doseq=True`; preserve the observed duplicate `version_code` keys and their order.
- No chat POST or delivery claim after failed enter, fetch, route validation, or WSS readiness; comments require separate user authorization.
- Preserve the dirty checkout; do not reset, clean, or commit user-owned changes.

## Review Focus

1. UA Chrome 150 with only `chrome145` transport available must fail before network I/O; Task 1 tests this.
2. A signer reused after changing account or room must reject the operation; Task 2 tests this.
3. A successful enter followed by fetch with stale/unproven SDK state must prevent WSS and chat; Tasks 2 and 4 test this.
4. Query `msToken` and Cookie `msToken` can differ; token rotation must not overwrite one with the other; Task 3 tests this.
5. A 200 response with empty JSON, wrong content type, missing cursor, or invalid WSS host must fail closed; Task 4 tests this.

---

## File map

- `tiktok_live/config.py`: strict browser-profile selection for this flow; existing permissive profile lookup can remain for unrelated paths until migration.
- `tiktok_live/signing_session.py` (new): per-account/page signer state, checked transitions, counters, and token provenance; no HTTP.
- `tiktok_live/signing.py`, `tiktok_live/algorithms/x_dynosaur.py`, `x_gnarly.py`, `ms_token.py`: pure signing primitives with explicit context inputs rather than endpoint defaults in the new path.
- `tiktok_live/client.py`: shared sender lifecycle, fresh report/enter sequence, and chat gate.
- `tiktok_live/wss/initial_fetch.py`, `sessions.py`, `connection.py`: first-party fetch, bounded decode, validated route, and WSS lifecycle.
- `tests/test_signing_profile.py`, `test_signing_session.py`, `test_signing_token_provenance.py`, `test_initial_fetch_request.py`, `test_initial_fetch_sessions.py`, `test_initialized_connection.py`: synthetic, no-network gates.

### Task 1: Coherent Browser Profile

**Files:** Modify `tiktok_live/config.py`; create `tests/test_signing_profile.py`.

**Interfaces:** Produce `strict_browser_profile_from_user_agent(user_agent: str) -> BrowserProfile`, raising `ValueError("unsupported_browser_profile")` if the major has no exact supported `curl_cffi` impersonation. Add `BrowserProfile.browser_version: str`, derived by removing only the leading `Mozilla/` from `user_agent`. Preserve `browser_profile_from_user_agent` for pre-migration callers.

- [ ] Write tests: UA 136 selects `chrome136`, generates `browser_version` from that same UA and Client Hints major 136; UA 150 and the current 146→145 fallback fail before a fake HTTP session is constructed; malformed UA fails.
- [ ] Run `python -m unittest tests.test_signing_profile -v`; expect import/assertion failure.
- [ ] Implement the strict selector using the existing `BrowserProfile` inventory; do not guess a nearest fingerprint.
- [ ] Re-run the test and `python -m unittest tests.test_saved_request_identity -v`; expect pass.

### Task 2: Isolated Signing State and Fixed-Input Parity

**Files:** Create `tiktok_live/signing_session.py`, `tests/test_signing_session.py`; modify `tiktok_live/signing.py`, `tiktok_live/algorithms/x_dynosaur.py`, `tiktok_live/algorithms/x_gnarly.py`.

**Interfaces:** Produce `SigningSession(account_id: str, room_id: str, page_url: str, profile: BrowserProfile, sdk_state: SdkState)` and `SdkState` with explicitly supplied, validated version, page seed, canvas, initialized fields, and counters. Produce `SigningSession.sign(url: str, *, account_id: str, room_id: str, query_token: str, body: bytes | None = None, operation: str) -> str`; it rejects another account/room, missing state, unsupported SDK version, and repeated operation, and advances only on successful signing. Extend `sign_url(..., dynosaur_context: dict | None = None, gnarly_context: dict | None = None)` and `get_x_gnarly(..., context: dict | None = None)` without changing the legacy defaults.

- [ ] Write synthetic tests for two account sessions, page seed/canvas continuity across enter and fetch, exact body-byte hashing, monotonic actual-request counters, and rejection of mixed identity or missing initialized state.
- [ ] Run `python -m unittest tests.test_signing_session -v`; expect failure.
- [ ] Pass explicit contexts into both Python algorithms. Verify fixed-input X-Dynosaur/X-Gnarly decoded fields against the previously observed browser metadata; no captured secret signature in fixtures. For field40, field51, canvas generation, and SDK version sourcing, record the proven derivation in the test name or stop at `signing_state_unsupported`; do not replace unknowns with constants. A local JS helper is allowed only for a separately demonstrated deterministic field and must have no network/browser API.
- [ ] Re-run the new tests and `python -m unittest tests.test_entry_signature_context -v`; revise old tests that assert endpoint-fixed `11/40` so they cover legacy behavior only, not the new session.

### Task 3: Token Provenance and Response Rotation

**Files:** Modify `tiktok_live/signing_session.py`, `tiktok_live/algorithms/ms_token.py`, `tiktok_live/client.py`; create `tests/test_signing_token_provenance.py`.

**Interfaces:** Produce `SigningSession.set_query_token(token: str, source: str) -> None`, `SigningSession.observe_response(origin: str, set_cookie_headers: tuple[str, ...]) -> None`, and `SigningSession.cookie_header_for(url: str) -> str`. Query token and Cookie jar remain independent. Only a validated first-party origin may update the account jar.

- [ ] Write tests proving a 172-character synthetic query token and different synthetic Cookie token can coexist, allowed Set-Cookie rotation changes only the jar, wrong origin cannot set a TikTok Cookie, and no raw token appears in errors/repr.
- [ ] Run `python -m unittest tests.test_signing_token_provenance -v`; expect failure.
- [ ] Trace the first-party SDK's token writer/refresh path using fixed inputs and record which value signs each request. Keep `/web/report` as one candidate issuer, not an assumed replacement for every webpage token. If the query-token provenance remains unknown, fail at `query_token_unsupported` rather than copying a captured value.
- [ ] Implement source-aware token handling and re-run the new tests plus `tests.test_chat_csrf_flow`; expect pass.

### Task 4: First-Party Fetch and WSS Gate

**Files:** Modify `tiktok_live/wss/initial_fetch.py`, `sessions.py`, `connection.py`, `tiktok_live/client.py`; complete `tests/test_initial_fetch_request.py`; create `tests/test_initial_fetch_sessions.py`, `tests/test_initialized_connection.py`.

**Interfaces:** Produce `build_initial_fetch_request(session: DirectSession, *, signer: SigningSession, endpoint: str) -> tuple[str, dict[str, str]]` and `fetch_initial_response(session: DirectSession, *, signer: SigningSession, endpoint: str, timeout_seconds: float) -> InitialFetch`. Consume `decode_initial_fetch` and `build_initialized_ws_url` from the existing first-party plan. No static-WSS or sign-service fallback.

- [ ] Write failing request tests for the 33 ordered query names (including two `version_code` entries encoded with `urlencode(..., doseq=True)`), `client_enter=1`, `ws_direct=1`, `cursor=0`, matching UA/room and token provenance, fresh signing, and rejection of a changed account or unsupported state.
- [ ] Write failing lifecycle tests: `enter → fetch → validated WSS → readiness → chat`; a 200 empty JSON, wrong content type, malformed protobuf, missing route, cancelled fetch, or rejected enter prevents both WSS and chat.
- [ ] Run `python -m unittest tests.test_initial_fetch_request tests.test_initial_fetch_sessions tests.test_initialized_connection -v`; expect failures.
- [ ] Implement bounded fetch and connect using the same account session; preserve isolated anonymous observer bootstrap and existing ACK/heartbeat behavior. Do not silently repeat enter or treat initial historical messages as delivered comments.
- [ ] Run the targeted tests plus `tests.test_direct_protocol`, `tests.test_direct_sessions`, `tests.test_room_enter_before_chat`; expect pass.

### Task 5: Evidence-Gated Live Verification

**Files:** Update `docs/third-party/tiktokapis-wss.md` with proven behavior only; no new runtime interface.

- [ ] Run targeted and full `python -m unittest discover -s tests -v`; separate pre-existing failures from regressions.
- [ ] With separately scoped read-only authorization and a currently live room, verify the saved account obtains two fresh accepted browser-free fetch responses with nonempty cursor and validated route. Log only HTTP/business status, content type, byte length, and safe field metadata.
- [ ] Verify WSS upgrade, server frames, bounded heartbeat/ACK, and stop/cancellation. No chat POST if any gate fails.
- [ ] Only with separately authorized live-message testing, send one marked comment and independently observe sender, content, message ID, and time. Report observed/not observed; HTTP 200 alone is not success.
- [ ] Review all touched files for credentials, captured dynamic values, unrelated diffs, browser-driving runtime code, and third-party signing-service calls. Do not build or commit a release without ownership review.

## Execution gates

Task 2's unknown SDK fields and Task 3's token provenance are explicit research gates: if fixed-input parity cannot be demonstrated, stop with an unsupported-state diagnostic rather than proceeding to an apparently working send path. Task 5 requires action-time authorization for account-bound network replay and separate authorization for posting a comment. Continue within the user's chosen current checkout; no new worktree is needed.
