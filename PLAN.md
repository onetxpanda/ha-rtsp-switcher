# Multi-Camera Pipeline Architecture Plan

## Goal

Keep all configured RTSP input streams running simultaneously. Switch between them
instantly by changing which one feeds the single decode/encode/RTMP pipeline, instead
of tearing down and restarting the whole pipeline on every switch.

---

## Current Architecture

```
PipelineManager (thread)
  └─ pipeline_worker (process, one at a time)
       rtspsrc → rtpXdepay → Xparse → decode → encode → flvmux → rtmpsink
                                                       ↘ snapshot appsink
                                                       ↘ preview appsink (H.264 only)
```

Switch = SIGTERM to current process, spawn new process, wait for RTSP connect + IDR.
~5s gap minimum.

---

## New Architecture

```
InputProcess × N  (one per configured camera, always running)
  rtspsrc → appsink  →  multiprocessing.Queue(maxsize=60, leaky)

Main process (feeder threads, one per camera)
  queue.get() → if active_idx matches → push to appsrc → [main pipeline]

MainPipelineProcess (one, always running)
  appsrc → rtpXdepay → Xparse → decode → encode → flvmux → rtmpsink
                                                 ↘ snapshot appsink
                                                 ↘ preview appsink (H.264 only)
```

---

## Components

### InputProcess

- `multiprocessing.Process`, one per camera, managed by `StreamManager`
- GStreamer pipeline (no decode, no depay):
  ```
  rtspsrc name=src location=<url> protocols=tcp latency=<ms>
    ! appsink name=sink emit-signals=false sync=false max-buffers=60 drop=true
  ```
- Output of rtspsrc is already jitter-buffered RTP (`application/x-rtp` caps)
- Internal reader thread: polls appsink, puts raw RTP buffers on its
  `multiprocessing.Queue(maxsize=60)` — bounded, old packets drop when full
- If RTSP disconnects: restarts its own mini-pipeline in a loop with reconnect_delay,
  independent of everything else
- Does not need to know which camera is active

### Feeder Threads (in main process)

- N threads, one per camera, each blocking on `queue.get()`
- All run continuously
- Each checks `active_idx[0] == own_idx` before pushing to appsrc
- If not active: packet is discarded
- Switching is a single integer write — GIL makes CPython int assignment atomic:
  ```python
  active_idx[0] = new_idx
  ```
- No GStreamer pad changes, no locking, no coordination

### MainPipelineProcess

- Single `multiprocessing.Process`, always running
- Built for the active camera's codec at start time
- appsrc caps set from config:
  - H.264: `application/x-rtp,media=video,clock-rate=90000,encoding-name=H264`
  - H.265: `application/x-rtp,media=video,clock-rate=90000,encoding-name=H265`
- `appsrc` properties: `is-live=true format=time do-timestamp=true`
  - `do-timestamp=true` discards original camera timestamps, uses pipeline clock —
    ensures timestamp continuity across camera switches
- Rest of pipeline unchanged from current: depay → parse → decode → encode → RTMP,
  snapshot tap, preview tap (H.264 only)
- Stall/startup timeout watchdog unchanged, watches encoder output

### StreamManager (replaces PipelineManager)

Responsibilities:
- Owns the list of `InputProcess` objects and the `MainPipelineProcess`
- Owns `active_idx: list[int]` (single-element list for mutability from threads)
- Owns N `multiprocessing.Queue` objects, one per camera
- Owns N feeder threads

On startup:
1. Spawn one `InputProcess` per configured camera
2. Start N feeder threads
3. Build `MainPipelineProcess` for the first camera's codec
4. Set `active_idx[0] = 0`

`switch_stream(name)`:
1. Look up new camera index and codec
2. If codec == current codec:
   - Update `active_idx[0] = new_idx` — instant, no pipeline touch
3. If codec != current codec:
   - Update `active_idx[0] = new_idx` first (input starts buffering)
   - Rebuild `MainPipelineProcess` with new codec
   - Input processes keep running throughout

`restart()` (config change — streams added/removed/modified):
- Tear down all `InputProcess` objects and `MainPipelineProcess`
- Drain all queues
- Respawn everything from current config

---

## Codec Switching

| Scenario | Result |
|---|---|
| Switch between two H.264 cameras | Instant — `active_idx` update only |
| Switch between two H.265 cameras | Instant — `active_idx` update only |
| Switch H.264 → H.265 or vice versa | ~1–2s gap — rebuild MainPipelineProcess |

Cross-codec rebuilds are faster than today because input processes are already running:
no RTSP reconnect wait, packets are buffered and ready to feed the moment the new
pipeline comes up.

---

## What Does Not Change

- `_build_pipeline_string` — used for main pipeline, just loses the `rtspsrc` source
  element (replaced by `appsrc`)
- Snapshot loop (`_snapshot_loop`)
- Video preview loop (`_video_loop`)
- YouTube manager
- HA listener
- Web UI, all API endpoints
- Stall/startup timeout watchdog logic

---

## Open Questions (confirm before implementation)

1. `do-timestamp=true` drops original camera PTS — preview timestamps in browser lose
   accuracy. Acceptable?

2. If active camera's input process loses RTSP and restarts, main pipeline stall
   detector may fire and also restart main pipeline. Both restarting independently is
   fine — agree?

3. Max number of cameras expected? Affects whether queue memory usage needs attention.
