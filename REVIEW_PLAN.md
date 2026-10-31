# Code Review Follow-ups

Outstanding findings from the 2026-10-31 codebase review. Items 1 (stale client config wiping the
refresh token) and 2 (auto-restart orphan broadcasts / quota burn) were fixed in v4.1.7, which also
added a restart lock covering the old item 5 (concurrent restarts creating duplicate broadcasts).

Line references are to `rtsp_switcher/rtsp_switcher.py` as of v4.1.7 and will drift.

Several items (3, 4, 6) go away naturally if the multi-camera architecture in `PLAN.md` lands
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

### 6. Saving unrelated config restarts the pipeline (drops the YouTube stream)
- `POST /api/config` always calls `_manager_ref.restart()`. Saving OAuth credentials, or HA settings,
  briefly kills RTMP output.
- **Fix:** diff old vs new config and only restart when pipeline-relevant keys change
  (`rtmp_url`, `output_*`, `video_bitrate_kbps`, `rtsp_latency_ms`, `video_preview`, and the
  active stream's entry in `streams`).

### 7. Video preview spinner never clears after a camera switch
- `VideoPreview`'s effect has `[]` deps; the decoder `output` callback closes over the initial
  `loading` (`false`) and `onFirstLoad`, so after a pipeline-generation change `onFirstLoad()` is
  never called and the overlay stays on top of the video.
- **Fix:** mirror `loading`/`onFirstLoad` into refs (like `pipelineGenRef`) and read the refs in
  the callback.

### 8. HA `select_option` from the web thread likely races the listener (unverified)
- `/api/switch` calls `_ha_listener_ref.select_option()` on a Flask thread, using the same
  `WebsocketClient` the listener thread is blocked reading in `listen_events`. Concurrent reads on
  one websocket typically raise or steal each other's messages; the `except: pass` fallback hides it.
- **Verify first:** check `homeassistant_api` WebsocketClient thread-safety; log the exception in
  the fallback instead of swallowing it.
- **Fix options:** use the HA REST API (`POST /api/services/input_select/select_option`) for the
  write, or a second dedicated client, or queue the call onto the listener thread.
- Related: `_sync_entity` only runs on connect, so cameras added/renamed in the UI aren't pushed to
  the `input_select` options until reconnect. Call it (via the same safe path) after config saves
  that change `streams`.

### 9. H.265 camera + preview enabled → stuck "Waiting for keyframe…"; WS thread leak
- The preview branch is only built for H.264, but the UI still opens the WebSocket and waits
  forever with no snapshot fallback.
- `_ws_video` only notices a disconnected client when `ws.send` fails, so with no frames flowing it
  leaks one thread per page load.
- **Fix:** expose whether preview is available in `/api/status` (e.g. `preview_available`) and fall
  back to snapshots when false, or fall back client-side after N seconds without a keyframe.
  Server side, periodically send a tiny keepalive/ping so dead sockets are detected.

### 10. Preview frame drops corrupt decoding until next IDR
- `video_queue.put_nowait` and the per-client drop-oldest in `_drain_video_frames` drop arbitrary
  H.264 frames; dropped reference frames cause artifacts until the camera's next IDR (camera GOP,
  can be several seconds).
- **Fix:** once a frame is dropped for a client, mark it "needs keyframe" and discard deltas until
  the next keyframe (server side), or send a flag so the client resets its decoder.

---

## Low / hygiene

- **Startup race:** `main()` calls `manager.switch_stream(streams[0])` after starting the HA listener;
  whichever wins decides the initial camera, which can disagree with HA's `input_select`. Only
  default to `streams[0]` when HA isn't configured, or after HA initial state fails.
- **Secrets exposure:** `GET /api/config` returns HA token, YouTube client secret, refresh token, and
  RTMP stream key; Flask listens on `0.0.0.0:8099` without auth (reachable from other containers on
  the hassio network). Redact secrets in GET (send a placeholder, keep existing on POST when the
  placeholder comes back) and/or bind/verify ingress source (`172.30.32.2`).
- **Unpinned CDN scripts:** `react@18`, `react-dom@18`, and unversioned `@babel/standalone` from
  unpkg. Pin exact versions at minimum; better, precompile the JSX and vendor React into the image
  so the UI works offline and loads faster.
- **Numeric fields can save as 0:** `num()` in `SettingsTab` maps empty input to `0`;
  `output_stall_timeout_sec: 0` kills the pipeline every second. Validate/clamp server-side in
  `POST /api/config` (also guards against missing keys causing worker `KeyError` crash loops).
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
- **SyntaxWarning on startup:** the embed-mode JS regex in `_WEBUI_HTML` (`/rgb[a]?\(.../`) is an
  invalid Python escape in a non-raw string. Escape as `\\(` or make the HTML a raw string.
- **Duplicate camera names** are allowed in the UI but names are used as IDs. Reject duplicates in
  `CameraModal`/server-side.
