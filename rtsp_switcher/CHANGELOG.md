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
