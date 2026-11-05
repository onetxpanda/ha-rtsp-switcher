# Code Review Follow-ups

Outstanding findings from the 2026-10-31 codebase review. Items 1 (stale client config wiping the
refresh token) and 2 (auto-restart orphan broadcasts / quota burn) were fixed in v4.1.7, which also
added a restart lock covering the old item 5 (concurrent restarts creating duplicate broadcasts).
v4.4.0 (UI review) fixed items 6 (pipeline restarted on unrelated saves), 7 (preview spinner stuck
after a switch), 9 (H.265 preview / websocket thread leak) and 10 (preview frame drops), plus the low
items on secrets exposure / unauthenticated port, unpinned CDN scripts and numeric validation.
`_sync_entity` now also runs after config saves that rename or add cameras.

Line references are to `rtsp_switcher/rtsp_switcher.py` as of v4.1.7 and will drift.

Items 3 and 4 go away naturally if the multi-camera architecture in `PLAN.md` lands
(pipeline stays up across switches) — decide whether to fix them now or fold them into that work.

---

## Medium

### 3. Every camera switch waits an extra `reconnect_delay_sec`
- `pipeline_worker` handles SIGTERM itself (`on_sigterm`) then `sys.exit(exit_code[0])` → exit code `0`.
- `PipelineManager.run()` checks `p.exitcode == -signal.SIGTERM` to detect an intentional switch;
  that branch never fires, so every switch/config restart sleeps `reconnect_delay_sec` (default 2s).
- **Fix:** have the manager record that it requested termination (e.g. a `_terminate_requested`
  flag set in `_terminate_current`, cleared on process start) and skip the delay when set.
  Alternatively exit with a distinct code from `on_sigterm`.

### 4. Lost switch race in `PipelineManager.run()`
- `stream_name` is read from `_current_stream`, then the process is started, and only afterwards
  stored in `self._process`. A `switch_stream()` in that window terminates the old (dead) process;
  the new one keeps running the old camera while UI/HA show the new one.
- **Fix:** after assigning `self._process`, re-check under the lock:
  `if self._current_stream != stream_name: p.terminate()`.

### 8. HA `select_option` from the web thread likely races the listener (unverified)
- `/api/switch` calls `_ha_listener_ref.select_option()` on a Flask thread, using the same
  `WebsocketClient` the listener thread is blocked reading in `listen_events`. Concurrent reads on
  one websocket typically raise or steal each other's messages; the `except: pass` fallback hides it.
- **Verify first:** check `homeassistant_api` WebsocketClient thread-safety; log the exception in
  the fallback instead of swallowing it.
- **Fix options:** use the HA REST API (`POST /api/services/input_select/select_option`) for the
  write, or a second dedicated client, or queue the call onto the listener thread.

---

## Low / hygiene

- **Startup race:** `main()` calls `manager.switch_stream(streams[0])` after starting the HA listener;
  whichever wins decides the initial camera, which can disagree with HA's `input_select`. Only
  default to `streams[0]` when HA isn't configured, or after HA initial state fails.
- **Pipeline string quoting:** `rtmpsink location={cfg['rtmp_url']}` is unquoted; `_quote_uri`
  doesn't escape backslashes. Quote and escape both.
- **Non-atomic config write:** `_save_config` writes in place; write to a temp file in the same
  directory then `os.replace()`.
- **Dead addon options:** `config.yaml` `options`/`schema` are never read and use different key
  names (`name`/`url` vs `stream_name`/`stream_url`); defaults contain a personal camera IP/entity.
  Remove them (or set `options: {}` / `schema: {}`) so the HA Configuration tab doesn't mislead.
- **`full_access: true`:** broader than needed for `/dev/dri`. Try `video: true` or
  `devices: [/dev/dri/renderD128]` instead.
- **`HomeAssistantListener._stop` shadows `threading.Thread._stop()`:** harmless today, breaks if
  `join()`/`is_alive()` is ever called. Rename to `_stop_event`.
- **Transient network errors classified as auth errors:** a DNS/connection failure in
  `_refresh_access_token` yields "No access token", which `_poll` treats as auth failure and flips
  status to Idle. Distinguish `invalid_grant`/401 from network errors.
- **Device-flow `slow_down`:** treated as fatal in `/api/youtube/auth/poll`. Treat as pending (and
  ideally increase the client poll interval).
- **Duplicate camera names** are allowed in the UI but names are used as IDs. Reject duplicates in
  `CameraModal`/server-side.
