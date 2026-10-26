# Anonymous observer SOCKS5 implementation plan

**Goal:** Add an optional runtime SOCKS5 field used only by anonymous bootstrap and WSS, including reconnects.

**Approved design:** User confirmed the preceding short design: field in 运行设置 / 监听连接; accept host:port:user:password; mask credentials; blank retains existing network selection; explicit proxy failure never falls back to direct; account fetch/send/heartbeat unchanged.

**Architecture:** DesktopSettings passes observer_proxy through runner to LiveMessageListener and prepare_observer. A separate normalizer accepts shorthand and SOCKS5 URLs, normalizing to socks5h (remote DNS). Only anonymous initialization uses it. Socket transport adds an owned SOCKS socket before existing verified TLS and isolated handshake.

**Tech stack:** Python, PySide6, curl_cffi, websocket-client, PySocks.

**Constraints:** Work in current checkout as previously requested; preserve unrelated dirty changes; no saved account access, comment POSTs, EXE replacement, or commits in this task. Do not embed the supplied proxy credentials in source/tests/docs. Existing room-metadata account lookup remains unchanged. Local settings persistence follows existing runtime settings conventions; warn that stored proxy credentials are sensitive.

## Tasks

- [x] RED: tests in tests/test_observer_proxy.py for normalization, rejection without credential leaks, settings persistence/masking/read-only validation, anonymous bootstrap propagation, unchanged account route, runner propagation, and owned SOCKS transport using a local test peer.
- [x] GREEN: implement tiktok_live/wss/proxy.py; desktop/settings_store.py and desktop/dialogs.py; observer-only runner/listener wiring; SOCKS branch in isolated_socket.py; declare PySocks dependency.
- [x] Verify: run focused tests, existing listener/socket/settings/runner tests, then full unittest discovery with UTF-8 environment. Anonymous live check through provided proxy only; no account cookies or messages. Inspect final scoped diff.

## Review focus

Invalid ports, credentials with reserved characters, blank legacy settings, reconnect proxy retention, cancellation/resource cleanup, and no proxy propagation to account sessions. Tests must exercise these behaviors. Errors must never echo the URL/password.

## Execution record

Proceeding with confirmed scope in current checkout. Implementation and tests will be completed inline; no product release requested.

- Initial RED showed missing proxy field/normalizer/plumbing; after implementing, 6 tests passed.
- Transport RED showed HTTP tunnel instead of SOCKS and cancellation failure. Windows reproduction showed raw sockets cancelled promptly but PySocks makefile reads did not. Added deadline-bounded raw reads in DeadlineSocksSocket; focused suite 42/42 passed.
- Fresh independent reviewer found credential-length and shorthand delimiter edge cases. Added failing tests, then limited credential length to 127 UTF-8 bytes (PySocks 1.x wire encoding limitation) and matched URI scheme only at the start. New feature suite 13/13 passed.
- Initial full suite 723/723 passed in 276.862 seconds; final full suite after edge fixes 725/725 passed in 256.741 seconds (build/work/observer-proxy-final-full.log).
- Live check: samruayv.2 had ended (status 4); stef_gamingg was live. Via production Settings -> listener -> SOCKS bootstrap/WSS path, connected and received 18 comments in 30 seconds without reconnect, then closed. No account or message POST used. Credentials not persisted.
- UI rendered and inspected at build/work/observer-proxy-settings.png; input, conceal/reveal toggle, scope notice, buttons visible.
- Compared last release source snapshot: client.py, heartbeat.py, parallel_runner.py and public_room.py unchanged byte-for-byte. Runner edits only add observer option validation and forwarding. EXE unchanged.
