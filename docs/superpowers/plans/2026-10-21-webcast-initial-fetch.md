# First-Party Webcast Initial Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the direct-WSS static initialization with TikTok's first-party `/webcast/im/fetch/` response, without a third-party signing service.

**Architecture:** A bounded HTTP bootstrap obtains server-issued cursor, WSS destination, and route parameters in the selected account or isolated visitor session. The existing connection consumes a validated prepared result; failures prevent chat POST and never fall back to the static URL.

**Tech Stack:** Python 3.10+, `curl_cffi`, existing pure-Python protobuf framing and request signing, `websocket-client`, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-21-webcast-initial-fetch-design.md`

## Global Constraints

- No TikTokLive runtime or third-party signing-service request; no browser automation in the final send path.
- No captured Cookie, token, signature, protobuf body, or complete URL in source, fixtures, logs, or committed artifacts.
- Account heartbeat and anonymous observer use isolated sessions; verify destination before any credential-bearing HTTP or WSS request.
- Preserve existing account data and unrelated dirty-tree edits. Do not commit overlapping user changes; request direction before committing any file with pre-existing changes.
- No real comment POST, release build, or delivery claim without separate live-test authorization.
- If the local request signer or WSS frontier state cannot be shown to work with fresh initialization, stop at that evidence gate; do not substitute fixed captured values.

## Review Focus

1. A `200` response whose business status rejects entry must prevent fetch/WSS/chat; Task 3 pins this.
2. A protobuf response with a cursor but missing `push_server` or route map must fail closed; Task 1 pins this.
3. A server-provided WSS URL with wrong room, host, port, or auth query must never receive a Cookie; Task 1 pins this.
4. A visitor cookie or initial history comment must never be mistaken for account identity or new delivery evidence; Tasks 1 and 3 pin this.
5. Cancellation between HTTP fetch and WSS upgrade must close owned resources without opening another socket; Task 4 pins this.

---

### Task 1: Bounded Initial-Response Decoder and Destination

**Files:**
- Create: `tiktok_live/wss/initial_fetch.py` — bounded protobuf parsing and immutable `InitialFetch` result.
- Modify: `tiktok_live/wss/sessions.py` — validate a server-issued WSS URL against room and allowlisted TikTok host/path.
- Test: `tests/test_initial_fetch.py`.

**Interfaces:**
- Produces: `decode_initial_fetch(raw: bytes, *, max_bytes: int = 2 * 1024 * 1024) -> InitialFetch`.
- `InitialFetch` has `cursor: str`, `push_server: str`, `route_params: tuple[tuple[str, str], ...]`, `internal_ext: str`, and `historical_message_count: int`; secret-bearing fields are excluded from `repr`.
- Produces: `build_initialized_ws_url(initial: InitialFetch, *, room_id: str, user_agent: str, additional_params: tuple[tuple[str, str], ...]) -> str` and invokes `validate_destination` before returning.

- [ ] Write synthetic protobuf tests for a valid response with field 2 cursor, field 7 map entries, field 10 push server, and field 1 history; assert only metadata is exposed in `repr`.
- [ ] Add tests for truncated varints, >2 MiB response, absent cursor/host/map, and malformed UTF-8; assert `ValueError` and no partial result.
- [ ] Add URL tests rejecting non-WSS, non-TikTok, non-443, wrong path/room, userinfo, credential-bearing query, and a redirect-like server URL.
- [ ] Run `python -m unittest tests.test_initial_fetch -v`; expect failures because the new interfaces do not exist.
- [ ] Implement the decoder using the existing bounded `protocol._fields` parser plus a 2 MiB input check in `initial_fetch.py`. Build the URL with percent-encoded route-parameter values, preserve ordered duplicate fixed query keys, and validate the final URL.
- [ ] Re-run `python -m unittest tests.test_initial_fetch tests.test_direct_protocol tests.test_direct_sessions -v`; expect all pass.

### Task 2: Reconstruct First-Party Fetch Request

**Files:**
- Modify: `tiktok_live/wss/initial_fetch.py` — exact first-party HTTP request builder and response reader.
- Modify: `tiktok_live/wss/direct_models.py` — add task-local `device_id` and `ms_token` fields to `DirectSession`, hidden from `repr`.
- Test: `tests/test_initial_fetch_request.py`.

**Interfaces:**
- Consumes: `DirectSession` and `decode_initial_fetch` from Task 1.
- Produces: `build_initial_fetch_request(session: DirectSession, *, device_id: str, ms_token: str, endpoint: str) -> tuple[str, dict[str, str]]` (signed URL and headers); `fetch_initial_response(session: DirectSession, *, device_id: str, ms_token: str, endpoint: str, timeout_seconds: float) -> InitialFetch`.

- [ ] At the account-bound live checkpoint, capture only field names/order, status, lengths, and safe hashes for one selected account's `room/enter` then `/webcast/im/fetch/`; confirm the same room/device/UA and the response's cursor/push-server/map. Do not save raw secrets. If unavailable, record the missing evidence and stop before connecting production.
- [ ] Before another account-bound replay, make the local signature session use one generated page-state pair across `/web/report`, `room/enter`, and `im/fetch`: the first SDK `V()` output maps to Gnarly field 8, the second to Dynosaur field 52. The local `_gnarly_field8` formula was confirmed with fixed-time/random browser output; do not reuse fixed captured numbers.
- [ ] Source Dynosaur field 44 and Gnarly perf data from the exact `wID.canvas` value included in the same report environment; their equality was verified anonymously. Compare the remaining Dynosaur/Gnarly envelope fields and SDK version against a fresh accepted browser fetch before claiming signer parity. The current target's anonymous enter returned business status 4003157, so a new accepted-room capture is needed for that comparison.
- [ ] Write tests from redacted anonymous and account request-shape metadata: require `client_enter=1`, `ws_direct=1`, `cursor=0`, matching room/device/UA, `resp_content_type=protobuf`, and the observed ordered query fields; reject absent token or account/room mismatch.
- [ ] Write tests for fresh per-request signing, no cross-origin redirects, response size/content-type bounds, timeout, and cancellation; assert no raw URL or Cookie in exception/log text.
- [ ] Run `python -m unittest tests.test_initial_fetch_request -v`; expect request-builder/import failures.
- [ ] Implement using `signing.sign_url` only after its output is verified against fresh first-party fetch admission. Keep cookie scope in the request session; do not flatten visitor and account jars or use a saved signature. Stop with `initial_fetch_signing_unsupported` if parity is not established.
- [ ] Re-run `python -m unittest tests.test_initial_fetch_request tests.test_saved_request_identity -v`; expect all pass.

### Task 3: Session Bootstrap and Request Order

**Files:**
- Modify: `tiktok_live/wss/sessions.py` and `tiktok_live/wss/visitor_api.py` — isolated account/observer bootstrap inputs.
- Modify: `tiktok_live/client.py` and `tiktok_live/heartbeat.py` — carry the existing account room/device/MS-token state into its WSS bootstrap without repeating `room/enter` or changing chat POST.
- Test: `tests/test_initial_fetch_sessions.py`; update `tests/test_direct_sessions.py` and `tests/test_room_enter_before_chat.py` for the new contract.

**Interfaces:**
- Produces: `prepare_account(username: str, room_id: str, cookie_header: str, user_agent: str, *, device_id: str, ms_token: str, proxy_setting: str = 'system') -> DirectSession`; `prepare_observer(...) -> DirectSession` remains anonymous and supplies its own device/token state.
- Consumes: `fetch_initial_response` from Task 2.

- [ ] Write tests proving account order `room/enter → im/fetch → WSS → chat`, same room/device/UA, and no second enter for the same prepared account session.
- [ ] Write tests proving failed business status, missing fetch state, or mismatched selected account prevents WSS and chat POST.
- [ ] Write tests proving parallel accounts and observer never share login cookies, fetch state, or initial historical comments.
- [ ] Run `python -m unittest tests.test_initial_fetch_sessions tests.test_room_enter_before_chat -v`; expect failures under current direct bootstrap.
- [ ] Pass the existing account device/token through `_post_prepared_chat`, `room_heartbeat`, and its session factory; keep the existing account `enter_live_room` before heartbeat. For the observer, create a visitor-only device/token and first-party entry in its isolated bootstrap; fail closed on nonzero business status. Keep both paths on one identity/room each.
- [ ] Re-run these tests plus `tests.test_direct_sessions` and `tests.test_parallel_runner`; expect all pass.

### Task 4: Replace Static WSS Initialization and Verify Lifecycle

**Files:**
- Modify: `tiktok_live/wss/connection.py` — consume `InitialFetch` and server-issued URL/cursor instead of `build_direct_url`.
- Preserve: `tiktok_live/wss/protocol.py` frame encoder; pass its existing `encode_enter_room(..., cursor=initial.cursor)` argument rather than changing the wire format without evidence.
- Test: `tests/test_initialized_connection.py`; update `tests/test_direct_connection.py`.

**Interfaces:**
- Consumes: `fetch_initial_response`, `build_initialized_ws_url`, `DirectSession`.
- Preserves: `DirectConnection.run(stop_event)` and public listener/evidence interfaces.

- [ ] Write a fake-HTTP/fake-socket test asserting fetch precedes upgrade, enter uses the fetched cursor, initial history is not emitted as new chat, and server-issued route params reach only the validated WSS destination.
- [ ] Write cancellation/failure tests: no socket after fetch cancellation; no chat after failed upgrade; no fallback to static direct URL or sign service.
- [ ] Run `python -m unittest tests.test_initialized_connection -v`; expect failures under current static URL path.
- [ ] Wire the prepared result into `DirectConnection` and preserve bounded heartbeat, ACK, fragment handling, and per-room evidence. Retain `build_direct_url` only as an isolated legacy test helper, not a runtime fallback.
- [ ] Run `python -m unittest tests.test_initialized_connection tests.test_direct_connection tests.test_heartbeat_logging -v`; expect all pass.

### Task 5: End-to-End Gate and Handoff

**Files:**
- Test: `tests/test_regression.py` plus existing full suite.
- Update: `docs/third-party/tiktokapis-wss.md` with only verified protocol provenance and remaining limitations.

**Interfaces:** No new runtime interface.

- [ ] Run targeted initialization, session-isolation, chat-order, connection, and listener tests; fix only failures caused by this change.
- [ ] Run the full `python -m unittest discover -s tests -v`; record pass/fail counts and keep unrelated failures separate.
- [ ] With a separately authorized account-bound live test, verify one fresh fetch and WSS connection on `@dyyoudaddy10`: successful upgrade, expected server frames, and bounded heartbeat/ACK. Compare request order without logging secrets.
- [ ] Only after separate authorization for a real comment, send one marked message and have an independent observer verify message ID, sender, content, and time; report `observed`, `not_observed`, or `listener_unavailable` rather than equating HTTP 200 with display.
- [ ] Review the diff for credentials, raw responses, unrelated edits, third-party signer calls, static-WSS fallback, and browser-driving runtime dependencies. Do not commit or publish a build without a separate clean ownership review.

## Execution gates

Task 2's account-bound live capture and Task 5's live connection/comment checks require action-time authorization under the protocol-recovery workflow. If signed-in fetch admission or initialized WSS signing is not reproducible, stop and report the exact missing state; do not proceed to an apparently successful send path. The existing worktree contains many pre-existing changes, so implementation must use surgical patches and must not reset, clean, or stage them wholesale.
