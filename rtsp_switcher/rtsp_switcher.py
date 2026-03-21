#!/usr/bin/env python3
import os
import pathlib
import signal
import threading
import time

import yaml
from flask import Flask
from homeassistant_api import WebsocketClient

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib


def _load_config():
    config_path = pathlib.Path(__file__).parent / "settings.yaml"
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
    from flask import render_template_string
    return render_template_string(_WEBUI_HTML, streams=STREAM_URLS)


def _start_webserver():
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    _flask_app.run(host="0.0.0.0", port=_WEBUI_PORT, debug=False, use_reloader=False)


def _quote_uri(uri):
    return '"' + uri.replace('"', '\\"') + '"'


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
                    print(f"Home Assistant initial state error: {exc}")
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
                print(f"Home Assistant websocket error: {exc}")
                time.sleep(2)
            finally:
                if hasattr(self._client, "__exit__"):
                    try:
                        self._client.__exit__(None, None, None)
                    except Exception:
                        pass


class RtspSwitcher:
    def __init__(self, on_bus_message):
        self._stream_names = [stream["stream_name"] for stream in STREAM_URLS]
        self._on_bus_message = on_bus_message
        self._stopping = False
        self._exit_scheduled = False
        self._last_output_time = None
        self._started_at = None
        self._watchdog_thread = None
        self._pipeline = None
        self._bus = None
        self._bus_handler_id = None
        self._current_name = None
        GLib.timeout_add_seconds(1, self._check_stalled_output)

    def _build_pipeline(self, stream):
        encoder = (
            f"nvh264enc name=enc bitrate={VIDEO_BITRATE_KBPS} "
            "preset=12 gop-size=30 repeat-sequence-header=true"
        )
        parser = "h264parse config-interval=1"
        caps = "video/x-h264,profile=high"
        mux = "flvmux"

        codec = stream.get("stream_codec", "h264").lower()
        if codec == "h265":
            depay = "rtph265depay"
            parser_in = "h265parse config-interval=1"
            decoder = "nvh265dec"
        else:
            depay = "rtph264depay"
            parser_in = "h264parse config-interval=1"
            decoder = "nvh264dec"

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
            f"{flip}videoconvert ! videoscale ! videorate ! "
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

    def _on_output_probe(self, _pad, _info):
        self._last_output_time = time.monotonic()
        return Gst.PadProbeReturn.OK

    def _check_stalled_output(self):
        if self._stopping:
            return False
        if not self._pipeline:
            return True
        now = time.monotonic()
        if self._last_output_time is None:
            if self._started_at and now - self._started_at >= STARTUP_OUTPUT_TIMEOUT_SEC:
                self.schedule_exit(
                    f"no output buffers for {STARTUP_OUTPUT_TIMEOUT_SEC}s after start"
                )
        elif now - self._last_output_time >= OUTPUT_STALL_TIMEOUT_SEC:
            self._last_output_time = now
            self.schedule_exit(
                f"no output buffers for {OUTPUT_STALL_TIMEOUT_SEC}s"
            )
        return True

    def start_watchdog(self):
        if self._watchdog_thread:
            return

        def _watchdog_loop():
            while not self._stopping:
                now = time.monotonic()
                last = self._last_output_time
                if last is None:
                    if self._started_at and now - self._started_at >= STARTUP_OUTPUT_TIMEOUT_SEC:
                        print(
                            f"Watchdog exit: no output buffers for "
                            f"{STARTUP_OUTPUT_TIMEOUT_SEC}s after start"
                        )
                        os._exit(1)
                elif now - last >= OUTPUT_STALL_TIMEOUT_SEC:
                    print(
                        f"Watchdog exit: no output buffers for "
                        f"{OUTPUT_STALL_TIMEOUT_SEC}s"
                    )
                    os._exit(1)
                time.sleep(1)

        self._watchdog_thread = threading.Thread(
            target=_watchdog_loop, daemon=True
        )
        self._watchdog_thread.start()

    def schedule_exit(self, reason):
        if self._stopping or self._exit_scheduled:
            return
        self._exit_scheduled = True
        print(f"Scheduling process exit: {reason}")

        def _do_exit():
            if self._stopping:
                return False
            if self._pipeline:
                self._pipeline.set_state(Gst.State.NULL)
            os._exit(1)
            return False

        GLib.timeout_add_seconds(RECONNECT_DELAY_SEC, _do_exit)

    def _set_pipeline(self, pipeline):
        if self._bus:
            self._bus.remove_signal_watch()
            if self._bus_handler_id is not None:
                self._bus.disconnect(self._bus_handler_id)
        self._pipeline = pipeline
        self._bus = None
        self._bus_handler_id = None
        if pipeline:
            self._bus = pipeline.get_bus()
            self._bus.add_signal_watch()
            self._bus_handler_id = self._bus.connect("message", self._on_bus_message)

    def start_stream(self, name):
        if name not in self._stream_names:
            raise ValueError(f"Unknown stream name: {name}")
        if self._current_name == name and self._pipeline:
            return
        self.stop_current()
        stream = next(s for s in STREAM_URLS if s["stream_name"] == name)
        pipeline_str = self._build_pipeline(stream)
        print(f"GStreamer pipeline: {pipeline_str}")
        pipeline = Gst.parse_launch(pipeline_str)
        enc = pipeline.get_by_name("enc")
        if enc:
            pad = enc.get_static_pad("src")
            if pad:
                pad.add_probe(Gst.PadProbeType.BUFFER, self._on_output_probe)
        self._exit_scheduled = False
        self._last_output_time = None
        self._started_at = time.monotonic()
        self._set_pipeline(pipeline)
        pipeline.set_state(Gst.State.PLAYING)
        self._current_name = name

    def stop_current(self):
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        self._set_pipeline(None)

    def stop(self):
        self._stopping = True
        self.stop_current()

    @property
    def pipeline(self):
        return self._pipeline


def main():
    os.environ.setdefault("LIBVA_DRIVER_NAME", "iHD")
    os.environ.setdefault("LIBVA_DRM_DEVICE", "/dev/dri/renderD128")
    os.environ.setdefault("GST_VAAPI_DRM_DEVICE", "/dev/dri/renderD128")
    Gst.init(None)

    loop = GLib.MainLoop()
    last_state = {"value": None}
    stopping = {"value": False}

    def _on_bus_message(_bus, message):
        msg_type = message.type
        if msg_type == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"GStreamer error from {message.src.get_name()}: {err} ({dbg})")
            if message.src == switcher.pipeline:
                switcher.schedule_exit(f"pipeline error: {err}")
        elif msg_type == Gst.MessageType.WARNING:
            warn, dbg = message.parse_warning()
            print(f"GStreamer warning from {message.src.get_name()}: {warn} ({dbg})")
        elif msg_type == Gst.MessageType.EOS:
            print("GStreamer EOS received")
            if message.src == switcher.pipeline:
                switcher.schedule_exit("pipeline EOS")
        elif msg_type == Gst.MessageType.STATE_CHANGED:
            if message.src == switcher.pipeline:
                old, new, pending = message.parse_state_changed()
                print(
                    f"Pipeline state: {old.value_nick} -> {new.value_nick} "
                    f"(pending {pending.value_nick})"
                )

    switcher = RtspSwitcher(_on_bus_message)

    def _on_ha_state(state):
        if state == last_state["value"]:
            return
        last_state["value"] = state
        try:
            switcher.start_stream(state)
        except ValueError as exc:
            print(f"Home Assistant state not found in STREAM_URLS: {exc}")

    def _handle_sigint(*_):
        if stopping["value"]:
            return
        stopping["value"] = True
        switcher.stop()
        if ha_listener:
            ha_listener.stop()
        loop.quit()
        os._exit(0)

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)
    if hasattr(GLib, "unix_signal_add"):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _handle_sigint)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _handle_sigint)

    web_thread = threading.Thread(target=_start_webserver, daemon=True)
    web_thread.start()

    switcher.start_stream(STREAM_URLS[0]["stream_name"])
    switcher.start_watchdog()
    ha_listener = None
    if HA_URL and HA_TOKEN:
        ha_listener = HomeAssistantListener(
            lambda state: GLib.idle_add(_on_ha_state, state)
        )
        ha_listener.start()

    try:
        loop.run()
    finally:
        if ha_listener:
            ha_listener.stop()
        switcher.stop()


if __name__ == "__main__":
    main()
