## 4.1.2
- Fix embed mode: disable body scrolling

## 4.1.1
- Remove accidentally included custom Lovelace card file

## 4.1.0
- Add `/embed` sub-URL that serves the same UI with embed mode activated server-side via `window.EMBED_MODE` (avoids fragile ingress token URLs or query string stripping)

## 4.0.1
- Fix restart broadcast: wait for broadcast to reach `ready` state after binding before calling transition — previously called transition while broadcast was still in `created` state, causing YouTube API "invalid transition" error
- Fix restart broadcast: `title` variable was undefined, causing a NameError after a successful transition that masked the success as an error

## 4.0.0
- Add embed mode: append `?embed` to the ingress URL to get a stripped view (video feed, YouTube status bar, camera switching only — no tabs, no edit/delete/add)
- Embed mode reads HA theme CSS variables from the parent window when loaded via ingress (same-origin), so the card matches the active HA theme automatically

## 3.0.2
- Fix VideoPreview: improve "VideoDecoder not supported" message to distinguish between non-secure context (HTTP) and genuinely unsupported browser

## 3.0.1
- Fix VideoPreview: decode IDR frame immediately after decoder init (was skipped, causing P-frame decode errors)
- Fix VideoPreview: move pipelineGenRef declaration before the useEffect that captures it
- Fix VideoPreview: use local monotonic timestamp counter when GStreamer PTS is zero
- Add visible status bar below preview showing live/connecting/error state with reason
- Add console.log at key WebSocket and decoder lifecycle points for debugging

## 3.0.0
- Add live H.264 video preview via WebCodecs API (disabled by default — enable in Settings > Advanced, then restart addon)
- Taps camera H.264 elementary stream directly before the hardware decoder — zero re-encoding overhead
- WebSocket endpoint /ws/video streams binary H.264 Annex-B frames to browser
- Browser decodes using VideoDecoder API; codec string auto-detected from camera SPS NAL unit
- Falls back to JPEG snapshot polling on WebSocket error or if VideoDecoder is unsupported
- Add flask-sock dependency for WebSocket support

## 2.2.6
- Status strip: show "Stream started Xd Xh Xm Xs ago" with seconds, updating every second
- Fix elapsed time to correctly display days, hours, minutes, and seconds

## 2.2.5
- Status strip: show elapsed time only (no start date), remove broadcast title
- Status strip: text outside the badge is now white

## 2.2.4
- Increase button size (padding + font-size) for Edit/Delete/action buttons
- Increase tab bar icon sizes and tab padding

## 2.2.3
- Increase all text sizes by 1px

## 2.2.2
- Rename addon to "Youtube Live Manager"
- Use YouTube logo (mdi:youtube) in HA sidebar

## 2.2.1
- Rename HA sidebar entry to "Youtube Live"

## 2.2.0
- Auto-detect stream resolution, codec, and framerate via ffprobe; these fields are no longer manually entered
- Add camera: detection runs automatically on save; save is blocked if detection fails (error shown in modal)
- Edit camera: shows current detected values; ↻ button re-probes the stream
- Remove orientation/rotation support from camera config and pipeline

## 2.1.8
- Show spinner overlay on snapshot when camera switch is in progress
- Spinner clears automatically when first new frame from the new pipeline arrives
- Drain stale snapshot queue before starting new pipeline so old frames never show after a switch

## 2.1.7
- Increase vibrancy of YouTube status strip colors and active camera row highlight

## 2.1.6
- Move Stream Output settings (RTMP URL, resolution, bitrate) from Settings tab to YouTube tab

## 2.1.5
- Remove custom fallback from resolution and bitrate dropdowns; presets only

## 2.1.4
- Replace width/height/framerate inputs with a single YouTube resolution dropdown (360p–4K/2160p60)
- Replace bitrate kbps input with Mbps dropdown (2–50 Mbps); values stored as kbps internally
- Framerate is now part of the resolution preset per YouTube's supported ingestion specs
- Shows "Custom" option if existing settings don't match any preset

## 2.1.3
- Color the YouTube status strip by state: green when live and healthy, yellow for warnings, red for errors/no-data, grey when idle

## 2.1.2
- Fix scrolling: apply flex layout to #root instead of body so the flex chain actually reaches the scrollable content area

## 2.1.1
- Show RTMP stream health in status strip: warning/error indicator when YouTube reports bad or missing stream data
- Show configuration issues from YouTube (with severity) in the YouTube tab status card
- Poll liveStream healthStatus and configurationIssues on every status check

## 2.1.0
- Replace tab text with icons (camera, YouTube logo, settings gear)
- Add persistent YouTube status strip below tab bar: live/idle badge, start time, elapsed duration, broadcast title
- YouTube tab shows start time / ended time in the status card
- Strip and YouTube tab share a single status poll (no duplicate requests)

## 2.0.3
- Broadcast restart now copies all writable settings from the last broadcast (contentDetails: dvr, encryption, embed, latency preference, monitor stream, captions type, projection, auto-start/stop; status: madeForKids; snippet: title, description)

## 2.0.2
- Fix YouTube API error: broadcastStatus and mine are mutually exclusive parameters

## 2.0.1
- Fix YouTube status not updating after auth (force-poll on Refresh button, poll wakes early via event)
- Surface API errors in UI instead of silently returning idle
- Add Refresh button to YouTube tab for immediate status check

## 2.0.0
- Add YouTube Live integration: monitor broadcast status, auto-restart stopped broadcasts
- Add device flow OAuth setup (no browser needed on the server)
- Add YouTube tab in UI with live/idle status badge, broadcast title, Restart Broadcast button, auto-restart toggle
- OAuth credentials (client_id, client_secret) and refresh token stored in settings.yaml
- Background polling every 30s; auto-restart recreates broadcast bound to existing liveStream

## 1.3.1
- Fix UI content getting clipped and unscrollable when window is resized (add min-height: 0 to flex content area, use height: 100% instead of 100vh)

## 1.3.0
- Redesigned UI with Camera and Settings tabs
- Full-width snapshot preview below tabs
- Camera list shows active stream with green Live indicator
- Tapping a camera row switches the stream
- Stream switches now route through HA input_select WebSocket (no extra HTTP connections)

## 1.2.3
- Fix snapshot preview flashing by not resetting image state between refreshes

## 1.2.2
- Fix API calls through HA ingress using X-Ingress-Path header injection

## 1.2.1
- Keep settings.yaml write inside config lock for atomicity

## 1.2.0
- Add React web UI with dynamic config editing
- Camera list CRUD (add, edit, delete) without restarting
- JPEG snapshot endpoint served from memory (no filesystem write)
- All config options editable from the UI and persisted to settings.yaml

## 1.1.5
- Fix HA service call: use trigger_service instead of call_service

## 1.1.4
- Sync input_select options from configured streams on HA connect

## 1.1.3
- Remove startup diagnostics from run.sh

## 1.1.2
- Add HA connection and stream switch logging

## 1.1.1
- Switch back to iHD VA-API driver for newer Intel GPUs (i965 only supports up to ~9th gen)

## 1.1.0
- Fix DRM device access in container using full_access: true in config.yaml
- Add full VA-API driver stack (iHD, i965, mesa) and vainfo for diagnostics
- Add gstreamer1.0-va package for vah264dec/vah264enc elements

## 1.0.0
- Initial release: RTSP-to-RTMP stream switcher as HA addon
- GStreamer pipeline with VA-API hardware acceleration
- Home Assistant input_select integration for stream switching
- Ingress web UI
- Settings stored in /config/rtsp_switcher/settings.yaml
