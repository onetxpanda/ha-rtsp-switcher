#!/usr/bin/env python3
import multiprocessing
import os
import pathlib
import signal
import sys
import threading
import time

import yaml
from flask import Flask, render_template_string
from homeassistant_api import WebsocketClient


def _load_config():
    ha_path = pathlib.Path("/config/rtsp_switcher/settings.yaml")
    local_path = pathlib.Path(__file__).parent / "settings.yaml"
    config_path = ha_path if ha_path.exists() else local_path
    with open(config_path) as f:
        return yaml.safe_load(f)


_cfg = _load_config()

STREAM_URLS = _cfg["streams"]
RTMP_URL = _cfg["rtmp_url"]
HA_URL = _cfg["ha_url"]
HA_TOKEN = _cfg["ha_token"]
HA_ENTITY_ID = _cfg["ha_entity_id"]
OUTPUT_WIDTH = _cfg.get("output_width", 2560)
OUTPUT_HEIGHT = _cfg.get("output_height", 1440)
VIDEO_BITRATE_KBPS = _cfg.get("video_bitrate_kbps", 10000)
OUTPUT_FRAMERATE = _cfg.get("output_framerate", 30)
RTSP_LATENCY_MS = _cfg.get("rtsp_latency_ms", 200)
RECONNECT_DELAY_SEC = _cfg.get("reconnect_delay_sec", 2)
OUTPUT_STALL_TIMEOUT_SEC = _cfg.get("output_stall_timeout_sec", 10)
STARTUP_OUTPUT_TIMEOUT_SEC = _cfg.get("startup_output_timeout_sec", 20)

_WEBUI_PORT = 8099

_WEBUI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RTSP Switcher</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
    background: #111318;
    color: #e2e3e8;
    padding: 24px;
  }
  h1 { font-size: 20px; font-weight: 500; margin-bottom: 20px; color: #fff; }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #1c1f27;
    border-radius: 8px;
    overflow: hidden;
  }
  th {
    text-align: left;
    padding: 12px 16px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #8b8fa8;
    background: #14161c;
    border-bottom: 1px solid #2a2d38;
  }
  td {
    padding: 12px 16px;
    border-bottom: 1px solid #22252f;
    color: #d4d5de;
    vertical-align: middle;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #21242e; }
  .mono { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; color: #a8c7fa; }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
    background: #1e3a5f;
    color: #7ab4f5;
  }
</style>
</head>
<body>
<h1>Configured Streams</h1>
<table>
  <thead>
    <tr>
      <th>Name</th>
      <th>URL</th>
      <th>Resolution</th>
      <th>Framerate</th>
      <th>Codec</th>
      <th>Rotation</th>
    </tr>
  </thead>
  <tbody>
    {% for s in streams %}
    <tr>
      <td>{{ s.stream_name }}</td>
      <td class="mono">{{ s.stream_url }}</td>
      <td>{{ s.stream_width }}&times;{{ s.stream_height }}</td>
      <td>{{ s.stream_framerate }} fps</td>
      <td><span class="badge">{{ s.stream_codec | upper }}</span></td>
      <td>{{ s.get("stream_rotation", 0) }}&deg;</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
</body>
</html>"""

_flask_app = Flask(__name__)


@_flask_app.route("/")
def _webui_index():
    return render_template_string(_WEBUI_HTML, streams=STREAM_URLS)


def _start_webserver():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    _flask_app.run(host="0.0.0.0", port=_WEBUI_PORT, debug=False, use_reloader=False)


def _quote_uri(uri):
    return '"' + uri.replace('"', '\\"') + '"'


def _build_pipeline_string(stream, hwaccel):
    parser = "h264parse config-interval=1"
    caps = "video/x-h264,profile=high"
    mux = "flvmux"

    codec = stream.get("stream_codec", "h264").lower()

    if hwaccel == "nvenc":
        encoder = (
            f"nvh264enc name=enc bitrate={VIDEO_BITRATE_KBPS} "
            f"gop-size={OUTPUT_FRAMERATE} preset=p5 repeat-sequence-header=true"
        )
        if codec == "h265":
            depay = "rtph265depay"
            parser_in = "h265parse config-interval=1"
            decoder = "nvh265dec"
        else:
            depay = "rtph264depay"
            parser_in = "h264parse config-interval=1"
            decoder = "nvh264dec"
    else:
        # VA-API: vaapih264enc uses keyframe-period and rate-control instead of
        # gop-size/preset/repeat-sequence-header. h264parse config-interval=1
        # handles SPS/PPS injection so repeat-sequence-header is not needed.
        encoder = (
            f"vaapih264enc name=enc bitrate={VIDEO_BITRATE_KBPS} "
            f"keyframe-period={OUTPUT_FRAMERATE} rate-control=vbr"
        )
        if codec == "h265":
            depay = "rtph265depay"
            parser_in = "h265parse config-interval=1"
            decoder = "vaapih265dec"
        else:
            depay = "rtph264depay"
            parser_in = "h264parse config-interval=1"
            decoder = "vaapih264dec"

    rotation = stream.get("stream_rotation", 0)
    if rotation == 90:
        flip = "videoflip method=clockwise ! "
    elif rotation == 180:
        flip = "videoflip method=rotate-180 ! "
    elif rotation == 270:
        flip = "videoflip method=counterclockwise ! "
    else:
        flip = ""

    parts = [
        f"rtspsrc name=src0 location={_quote_uri(stream['stream_url'])} "
        f"protocols=tcp tcp-timeout=30000000 latency={RTSP_LATENCY_MS} ! "
        "queue name=qsrc0 ! "
        f"{depay} name=depay0 ! {parser_in} name=parse0 ! "
        "queue name=preq0 ! "
        f"{decoder} name=dec0 ! "
        f"videoconvert ! {flip}videoscale ! videorate ! "
        f"video/x-raw,width={OUTPUT_WIDTH},height={OUTPUT_HEIGHT},"
        f"framerate={OUTPUT_FRAMERATE}/1 ! "
        "queue name=postq0 ! "
        f"{encoder} ! "
        f"{caps} ! {parser} ! {mux} streamable=true name=mux ! "
        f"rtmpsink location={RTMP_URL}",
        "audiotestsrc is-live=true wave=silence ! "
        "audio/x-raw,channels=2,rate=44100 ! "
        "audioconvert ! audioresample ! "
        "voaacenc bitrate=128000 ! aacparse ! mux.",
    ]
    return " ".join(parts)


def pipeline_worker(stream_name):
    """Runs in a child process. Owns GStreamer entirely. Exits when the pipeline stops."""
    os.environ.setdefault("LIBVA_DRIVER_NAME", "iHD")
    os.environ.setdefault("LIBVA_DRM_DEVICE", "/dev/dri/renderD128")
    os.environ.setdefault("GST_VAAPI_DRM_DEVICE", "/dev/dri/renderD128")

    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib

    Gst.init(None)

    hwaccel = os.environ.get("RTSP_HWACCEL", "vaapi")

    stream = next((s for s in STREAM_URLS if s["stream_name"] == stream_name), None)
    if stream is None:
        print(f"[worker] Unknown stream name: {stream_name!r}", flush=True)
        sys.exit(1)

    pipeline_str = _build_pipeline_string(stream, hwaccel)
    print(f"[worker] Starting pipeline for {stream_name!r} (hwaccel={hwaccel})", flush=True)

    try:
        pipeline = Gst.parse_launch(pipeline_str)
    except Exception as exc:
        print(f"[worker] Failed to parse pipeline: {exc}", flush=True)
        sys.exit(1)

    loop = GLib.MainLoop()
    exit_code = [0]
    last_output_time = [None]
    started_at = time.monotonic()

    def on_output_probe(_pad, _info):
        last_output_time[0] = time.monotonic()
        return Gst.PadProbeReturn.OK

    enc = pipeline.get_by_name("enc")
    if enc:
        pad = enc.get_static_pad("src")
        if pad:
            pad.add_probe(Gst.PadProbeType.BUFFER, on_output_probe)

    def check_stalled():
        now = time.monotonic()
        last = last_output_time[0]
        if last is None:
            if now - started_at >= STARTUP_OUTPUT_TIMEOUT_SEC:
                print(
                    f"[worker] No output for {STARTUP_OUTPUT_TIMEOUT_SEC}s after start",
                    flush=True,
                )
                exit_code[0] = 1
                pipeline.set_state(Gst.State.NULL)
                loop.quit()
                return False
        elif now - last >= OUTPUT_STALL_TIMEOUT_SEC:
            print(f"[worker] Output stalled for {OUTPUT_STALL_TIMEOUT_SEC}s", flush=True)
            exit_code[0] = 1
            pipeline.set_state(Gst.State.NULL)
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(1, check_stalled)

    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_bus_message(_bus, message):
        msg_type = message.type
        if msg_type == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"[worker] Error: {err} ({dbg})", flush=True)
            exit_code[0] = 1
            pipeline.set_state(Gst.State.NULL)
            loop.quit()
        elif msg_type == Gst.MessageType.WARNING:
            warn, _ = message.parse_warning()
            print(f"[worker] Warning: {warn}", flush=True)
        elif msg_type == Gst.MessageType.EOS:
            print("[worker] EOS received", flush=True)
            exit_code[0] = 1
            pipeline.set_state(Gst.State.NULL)
            loop.quit()
        elif msg_type == Gst.MessageType.STATE_CHANGED:
            if message.src == pipeline:
                old, new, pending = message.parse_state_changed()
                print(
                    f"[worker] State: {old.value_nick} -> {new.value_nick}"
                    f" (pending: {pending.value_nick})",
                    flush=True,
                )

    bus.connect("message", on_bus_message)

    def on_sigterm(*_):
        pipeline.set_state(Gst.State.NULL)
        loop.quit()

    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)

    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        bus.remove_signal_watch()

    sys.exit(exit_code[0])


class HomeAssistantListener(threading.Thread):
    def __init__(self, on_state):
        super().__init__(daemon=True)
        self._on_state = on_state
        self._stop = threading.Event()
        self._client = None

    def stop(self):
        self._stop.set()
        if self._client and hasattr(self._client, "__exit__"):
            try:
                self._client.__exit__(None, None, None)
            except Exception:
                pass

    def run(self):
        self._client = WebsocketClient(HA_URL, HA_TOKEN)
        while not self._stop.is_set():
            try:
                if hasattr(self._client, "__enter__"):
                    self._client.__enter__()
                try:
                    state_obj = self._client.get_state(entity_id=HA_ENTITY_ID)
                    state = getattr(state_obj, "state", None)
                    if state:
                        self._on_state(state)
                except Exception as exc:
                    print(f"[ha] Initial state error: {exc}")
                with self._client.listen_events("state_changed") as events:
                    for event in events:
                        if self._stop.is_set():
                            break
                        data = getattr(event, "data", None)
                        if isinstance(data, dict):
                            entity_id = data.get("entity_id")
                            new_state = data.get("new_state") or {}
                            state = new_state.get("state")
                        else:
                            entity_id = getattr(data, "entity_id", None)
                            new_state = getattr(data, "new_state", None)
                            state = getattr(new_state, "state", None) if new_state else None
                        if not entity_id or entity_id != HA_ENTITY_ID:
                            continue
                        if state:
                            self._on_state(state)
            except Exception as exc:
                if self._stop.is_set():
                    break
                print(f"[ha] Websocket error: {exc}")
                time.sleep(2)
            finally:
                if hasattr(self._client, "__exit__"):
                    try:
                        self._client.__exit__(None, None, None)
                    except Exception:
                        pass


class PipelineManager(threading.Thread):
    """Manages the pipeline worker process. Restarts on crash; switches streams on demand."""

    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._current_stream = None
        self._process = None
        self._stopping = threading.Event()

    def switch_stream(self, name):
        stream_names = [s["stream_name"] for s in STREAM_URLS]
        if name not in stream_names:
            print(f"[manager] Unknown stream: {name!r}")
            return
        with self._lock:
            if self._current_stream == name:
                return
            self._current_stream = name
        self._terminate_current()

    def _terminate_current(self):
        with self._lock:
            p = self._process
        if p and p.is_alive():
            p.terminate()

    def stop(self):
        self._stopping.set()
        self._terminate_current()
        with self._lock:
            p = self._process
        if p:
            p.join(timeout=5)
            if p.is_alive():
                p.kill()

    def run(self):
        while not self._stopping.is_set():
            with self._lock:
                stream = self._current_stream
            if stream is None:
                self._stopping.wait(0.5)
                continue

            p = multiprocessing.Process(
                target=pipeline_worker,
                args=(stream,),
                name=f"pipeline-{stream}",
            )
            p.start()
            print(f"[manager] Started pipeline process {p.pid} for {stream!r}")
            with self._lock:
                self._process = p

            p.join()

            if self._stopping.is_set():
                break

            if p.exitcode == -signal.SIGTERM:
                print(f"[manager] Pipeline {p.pid} terminated for stream switch")
            else:
                print(
                    f"[manager] Pipeline {p.pid} exited (code {p.exitcode}),"
                    f" restarting in {RECONNECT_DELAY_SEC}s"
                )
                self._stopping.wait(RECONNECT_DELAY_SEC)


def main():
    stop_event = threading.Event()

    def _handle_signal(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    manager = PipelineManager()
    manager.start()

    web_thread = threading.Thread(target=_start_webserver, daemon=True)
    web_thread.start()

    ha_listener = None
    if HA_URL and HA_TOKEN:
        ha_listener = HomeAssistantListener(manager.switch_stream)
        ha_listener.start()

    manager.switch_stream(STREAM_URLS[0]["stream_name"])

    stop_event.wait()

    print("[main] Shutting down")
    manager.stop()
    if ha_listener:
        ha_listener.stop()


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
