#!/usr/bin/env python3
import multiprocessing
import os
import pathlib
import signal
import sys
import threading
import time

import json

import yaml
from flask import Flask, Response, abort, jsonify, request
from homeassistant_api import WebsocketClient


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_cfg_lock = threading.Lock()
_cfg: dict = {}
_cfg_path: pathlib.Path | None = None


def _load_config():
    global _cfg, _cfg_path
    ha_path = pathlib.Path("/config/rtsp_switcher/settings.yaml")
    local_path = pathlib.Path(__file__).parent / "settings.yaml"
    _cfg_path = ha_path if ha_path.exists() else local_path
    with open(_cfg_path) as f:
        _cfg = yaml.safe_load(f)


def _save_config(new_cfg: dict):
    global _cfg
    with _cfg_lock:
        _cfg = new_cfg
        with open(_cfg_path, "w") as f:
            yaml.safe_dump(new_cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _get_cfg() -> dict:
    with _cfg_lock:
        return dict(_cfg)


# ---------------------------------------------------------------------------
# Snapshot state (main process only)
# ---------------------------------------------------------------------------

_snapshot_lock = threading.Lock()
_latest_snapshot: bytes | None = None


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

_WEBUI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RTSP Switcher</title>
<script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<!--INGRESS_PATH-->
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0f1117; --surface: #181b26; --surface2: #1e2130; --border: #272b3d;
  --text: #dde1f0; --muted: #6b6f8a; --accent: #5b8cf8; --danger: #e05555; --success: #3ecf8e;
}
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; }
.sidebar { width: 192px; min-width: 192px; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 0; }
.sidebar-header { padding: 22px 20px 18px; border-bottom: 1px solid var(--border); }
.sidebar-title { font-size: 13px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.nav-item { display: flex; align-items: center; padding: 11px 20px; cursor: pointer; color: var(--muted); font-size: 13px; font-weight: 500; border-left: 2px solid transparent; transition: color .12s, background .12s; }
.nav-item:hover { color: var(--text); background: var(--surface2); }
.nav-item.active { color: var(--accent); border-left-color: var(--accent); background: rgba(91,140,248,.07); }
.main { flex: 1; overflow-y: auto; padding: 32px 36px; }
h1 { font-size: 17px; font-weight: 600; margin-bottom: 24px; color: #fff; }
h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); margin-bottom: 14px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 16px; }
.snapshot-wrap { background: #000; border-radius: 6px; overflow: hidden; aspect-ratio: 16/9; display: flex; align-items: center; justify-content: center; margin-bottom: 14px; position: relative; }
.snapshot-wrap img { width: 100%; height: 100%; object-fit: contain; display: block; }
.snapshot-placeholder { color: var(--muted); font-size: 13px; position: absolute; }
.badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }
.badge-live { background: rgba(62,207,142,.12); color: var(--success); }
.badge-idle { background: rgba(107,111,138,.12); color: var(--muted); }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field-full { grid-column: 1 / -1; }
label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
input, select { background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; color: var(--text); font-size: 13px; outline: none; width: 100%; transition: border-color .12s; }
input:focus, select:focus { border-color: var(--accent); }
input[type="password"] { font-family: monospace; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; outline: none; transition: opacity .12s, filter .12s; white-space: nowrap; }
.btn:hover { filter: brightness(1.1); }
.btn:active { filter: brightness(.9); }
.btn-primary { background: var(--accent); color: #fff; }
.btn-danger { background: var(--danger); color: #fff; }
.btn-ghost { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }
.btn-sm { padding: 5px 11px; font-size: 12px; }
.row { display: flex; align-items: center; gap: 10px; }
.row-between { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.camera-list { display: flex; flex-direction: column; gap: 8px; }
.camera-row { display: flex; align-items: center; gap: 14px; padding: 14px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
.camera-name { font-weight: 600; font-size: 13px; color: #fff; }
.camera-meta { font-size: 11px; color: var(--muted); margin-top: 2px; font-family: monospace; }
.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.65); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 24px; width: 500px; max-width: 95vw; }
.modal-title { font-size: 15px; font-weight: 600; color: #fff; margin-bottom: 20px; }
.modal-footer { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
.toast { position: fixed; bottom: 24px; right: 24px; padding: 11px 18px; border-radius: 8px; font-size: 13px; font-weight: 500; z-index: 200; border: 1px solid; }
.toast-ok { background: rgba(62,207,142,.1); border-color: rgba(62,207,142,.3); color: var(--success); }
.toast-err { background: rgba(224,85,85,.1); border-color: rgba(224,85,85,.3); color: var(--danger); }
.divider { height: 1px; background: var(--border); margin: 20px 0; }
</style>
</head>
<body>
<div id="root"></div>
<script type="text/babel">
const { useState, useEffect, useCallback, useRef } = React;
const BASE = window.INGRESS_PATH || '';

// ── Sidebar ──────────────────────────────────────────────────────────────────
function Sidebar({ page, onNavigate }) {
  const items = [
    { id: 'live',     label: 'Live' },
    { id: 'cameras',  label: 'Cameras' },
    { id: 'settings', label: 'Settings' },
  ];
  return (
    <nav className="sidebar">
      <div className="sidebar-header">
        <div className="sidebar-title">RTSP Switcher</div>
      </div>
      {items.map(i => (
        <div key={i.id} className={`nav-item${page === i.id ? ' active' : ''}`} onClick={() => onNavigate(i.id)}>
          {i.label}
        </div>
      ))}
    </nav>
  );
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function Toast({ toast }) {
  if (!toast) return null;
  return <div className={`toast ${toast.ok ? 'toast-ok' : 'toast-err'}`}>{toast.msg}</div>;
}

// ── Snapshot image ────────────────────────────────────────────────────────────
function Snapshot({ ts }) {
  const [state, setState] = useState('loading'); // loading | ok | error
  useEffect(() => setState('loading'), [ts]);
  return (
    <div className="snapshot-wrap">
      <img
        src={`${BASE}/api/snapshot?t=${ts}`}
        style={{ display: state === 'ok' ? 'block' : 'none' }}
        onLoad={() => setState('ok')}
        onError={() => setState('error')}
        alt="Live snapshot"
      />
      {state !== 'ok' && (
        <span className="snapshot-placeholder">
          {state === 'loading' ? 'Loading\u2026' : 'No snapshot available'}
        </span>
      )}
    </div>
  );
}

// ── Live page ─────────────────────────────────────────────────────────────────
function LivePage() {
  const [status, setStatus] = useState(null);
  const [ts, setTs] = useState(Date.now());

  useEffect(() => {
    const tick = () => {
      fetch(`${BASE}/api/status`).then(r => r.json()).then(setStatus).catch(() => {});
      setTs(Date.now());
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => clearInterval(id);
  }, []);

  return (
    <div>
      <h1>Live</h1>
      <div className="card">
        <Snapshot ts={ts} />
        {status && (
          <div className="row">
            <span className={`badge ${status.streaming ? 'badge-live' : 'badge-idle'}`}>
              {status.streaming ? '\u25cf Live' : '\u25cb Idle'}
            </span>
            {status.active_stream && (
              <span style={{ color: 'var(--muted)', fontSize: 13 }}>{status.active_stream}</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Camera modal ──────────────────────────────────────────────────────────────
const BLANK_CAM = {
  stream_name: '', stream_url: '', stream_width: 1920, stream_height: 1080,
  stream_framerate: 30, stream_codec: 'h264', stream_rotation: 0,
};

function CameraModal({ initial, onSave, onClose }) {
  const [form, setForm] = useState(initial ? { ...initial } : { ...BLANK_CAM });
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const num = (k, v) => set(k, parseInt(v) || 0);

  return (
    <div className="modal-bg" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-title">{initial ? 'Edit Camera' : 'Add Camera'}</div>
        <div className="form-grid">
          <div className="field field-full">
            <label>Name</label>
            <input value={form.stream_name} onChange={e => set('stream_name', e.target.value)} placeholder="Inside Box" />
          </div>
          <div className="field field-full">
            <label>RTSP URL</label>
            <input value={form.stream_url} onChange={e => set('stream_url', e.target.value)} placeholder="rtsp://192.168.1.x:8554/stream" />
          </div>
          <div className="field">
            <label>Width</label>
            <input type="number" value={form.stream_width} onChange={e => num('stream_width', e.target.value)} />
          </div>
          <div className="field">
            <label>Height</label>
            <input type="number" value={form.stream_height} onChange={e => num('stream_height', e.target.value)} />
          </div>
          <div className="field">
            <label>Framerate</label>
            <input type="number" value={form.stream_framerate} onChange={e => num('stream_framerate', e.target.value)} />
          </div>
          <div className="field">
            <label>Codec</label>
            <select value={form.stream_codec} onChange={e => set('stream_codec', e.target.value)}>
              <option value="h264">H.264</option>
              <option value="h265">H.265</option>
            </select>
          </div>
          <div className="field">
            <label>Rotation</label>
            <select value={form.stream_rotation || 0} onChange={e => num('stream_rotation', e.target.value)}>
              <option value={0}>0\u00b0</option>
              <option value={90}>90\u00b0</option>
              <option value={180}>180\u00b0</option>
              <option value={270}>270\u00b0</option>
            </select>
          </div>
        </div>
        <div className="modal-footer">
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={() => onSave(form)}>Save</button>
        </div>
      </div>
    </div>
  );
}

// ── Cameras page ──────────────────────────────────────────────────────────────
function CamerasPage({ config, onConfigChange, showToast }) {
  const [modal, setModal] = useState(null); // null | 'add' | number (index)

  if (!config) return <div style={{ color: 'var(--muted)' }}>Loading\u2026</div>;
  const streams = config.streams || [];

  const persist = async (newStreams) => {
    const newCfg = { ...config, streams: newStreams };
    const ok = await saveConfig(newCfg);
    if (ok) { onConfigChange(newCfg); showToast('Saved', true); }
    else showToast('Save failed', false);
  };

  const handleSave = (form) => {
    const newStreams = modal === 'add'
      ? [...streams, form]
      : streams.map((s, i) => i === modal ? form : s);
    persist(newStreams);
    setModal(null);
  };

  const handleDelete = (idx) => {
    if (!confirm(`Delete "${streams[idx].stream_name}"?`)) return;
    persist(streams.filter((_, i) => i !== idx));
  };

  return (
    <div>
      <div className="row-between">
        <h1 style={{ margin: 0 }}>Cameras</h1>
        <button className="btn btn-primary" onClick={() => setModal('add')}>+ Add Camera</button>
      </div>
      {streams.length === 0 && (
        <p style={{ color: 'var(--muted)', fontSize: 13 }}>No cameras configured. Add one to get started.</p>
      )}
      <div className="camera-list">
        {streams.map((s, i) => (
          <div key={i} className="camera-row">
            <div style={{ flex: 1 }}>
              <div className="camera-name">{s.stream_name}</div>
              <div className="camera-meta">{s.stream_url}</div>
              <div className="camera-meta" style={{ fontFamily: 'sans-serif', marginTop: 3 }}>
                {s.stream_width}&times;{s.stream_height} &middot; {s.stream_framerate} fps &middot; {(s.stream_codec || '').toUpperCase()}
                {s.stream_rotation ? ` \u00b7 ${s.stream_rotation}\u00b0` : ''}
              </div>
            </div>
            <div className="row">
              <button className="btn btn-ghost btn-sm" onClick={() => setModal(i)}>Edit</button>
              <button className="btn btn-danger btn-sm" onClick={() => handleDelete(i)}>Delete</button>
            </div>
          </div>
        ))}
      </div>
      {modal !== null && (
        <CameraModal
          initial={modal === 'add' ? null : streams[modal]}
          onSave={handleSave}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  );
}

// ── Settings page ─────────────────────────────────────────────────────────────
function SettingsPage({ config, onConfigChange, showToast }) {
  const [form, setForm] = useState(null);
  useEffect(() => { if (config) setForm({ ...config }); }, [config]);

  if (!form) return <div style={{ color: 'var(--muted)' }}>Loading\u2026</div>;

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const num = (k, v) => set(k, parseInt(v) || 0);

  const handleSave = async () => {
    const ok = await saveConfig(form);
    if (ok) { onConfigChange(form); showToast('Saved', true); }
    else showToast('Save failed', false);
  };

  return (
    <div>
      <h1>Settings</h1>

      <div className="card">
        <h2>Stream Output</h2>
        <div className="form-grid">
          <div className="field field-full">
            <label>RTMP URL</label>
            <input value={form.rtmp_url || ''} onChange={e => set('rtmp_url', e.target.value)} placeholder="rtmp://a.rtmp.youtube.com/live2/..." />
          </div>
          <div className="field">
            <label>Output Width</label>
            <input type="number" value={form.output_width || ''} onChange={e => num('output_width', e.target.value)} />
          </div>
          <div className="field">
            <label>Output Height</label>
            <input type="number" value={form.output_height || ''} onChange={e => num('output_height', e.target.value)} />
          </div>
          <div className="field">
            <label>Video Bitrate (kbps)</label>
            <input type="number" value={form.video_bitrate_kbps || ''} onChange={e => num('video_bitrate_kbps', e.target.value)} />
          </div>
          <div className="field">
            <label>Output Framerate</label>
            <input type="number" value={form.output_framerate || ''} onChange={e => num('output_framerate', e.target.value)} />
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Home Assistant</h2>
        <div className="form-grid">
          <div className="field field-full">
            <label>WebSocket URL</label>
            <input value={form.ha_url || ''} onChange={e => set('ha_url', e.target.value)} placeholder="ws://homeassistant:8123/api/websocket" />
          </div>
          <div className="field field-full">
            <label>Long-Lived Access Token</label>
            <input type="password" value={form.ha_token || ''} onChange={e => set('ha_token', e.target.value)} />
          </div>
          <div className="field field-full">
            <label>Entity ID</label>
            <input value={form.ha_entity_id || ''} onChange={e => set('ha_entity_id', e.target.value)} placeholder="input_select.camera_view" />
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Advanced</h2>
        <div className="form-grid">
          <div className="field">
            <label>RTSP Latency (ms)</label>
            <input type="number" value={form.rtsp_latency_ms || ''} onChange={e => num('rtsp_latency_ms', e.target.value)} />
          </div>
          <div className="field">
            <label>Reconnect Delay (s)</label>
            <input type="number" value={form.reconnect_delay_sec || ''} onChange={e => num('reconnect_delay_sec', e.target.value)} />
          </div>
          <div className="field">
            <label>Output Stall Timeout (s)</label>
            <input type="number" value={form.output_stall_timeout_sec || ''} onChange={e => num('output_stall_timeout_sec', e.target.value)} />
          </div>
          <div className="field">
            <label>Startup Output Timeout (s)</label>
            <input type="number" value={form.startup_output_timeout_sec || ''} onChange={e => num('startup_output_timeout_sec', e.target.value)} />
          </div>
        </div>
      </div>

      <button className="btn btn-primary" onClick={handleSave}>Save Settings</button>
    </div>
  );
}

// ── Shared helpers ────────────────────────────────────────────────────────────
async function saveConfig(cfg) {
  try {
    const r = await fetch(`${BASE}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    return r.ok;
  } catch { return false; }
}

// ── App ───────────────────────────────────────────────────────────────────────
function App() {
  const [page, setPage] = useState('live');
  const [config, setConfig] = useState(null);
  const [toast, setToast] = useState(null);

  useEffect(() => {
    fetch(`${BASE}/api/config`).then(r => r.json()).then(setConfig).catch(() => {});
  }, []);

  const showToast = useCallback((msg, ok) => {
    setToast({ msg, ok });
    setTimeout(() => setToast(null), 3000);
  }, []);

  return (
    <>
      <Sidebar page={page} onNavigate={setPage} />
      <div className="main">
        {page === 'live'     && <LivePage />}
        {page === 'cameras'  && <CamerasPage config={config} onConfigChange={setConfig} showToast={showToast} />}
        {page === 'settings' && <SettingsPage config={config} onConfigChange={setConfig} showToast={showToast} />}
      </div>
      <Toast toast={toast} />
    </>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>
</body>
</html>"""

_flask_app = Flask(__name__)
_manager_ref = None


@_flask_app.route("/")
def _webui_index():
    ingress_path = request.headers.get("X-Ingress-Path", "")
    html = _WEBUI_HTML.replace(
        "<!--INGRESS_PATH-->",
        f"<script>window.INGRESS_PATH={json.dumps(ingress_path)};</script>",
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@_flask_app.route("/api/config")
def _api_config_get():
    return jsonify(_get_cfg())


@_flask_app.route("/api/config", methods=["POST"])
def _api_config_post():
    new_cfg = request.get_json(force=True)
    if not isinstance(new_cfg, dict):
        return jsonify({"error": "invalid body"}), 400
    try:
        _save_config(new_cfg)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if _manager_ref:
        _manager_ref.restart()
    return jsonify({"ok": True})


@_flask_app.route("/api/snapshot")
def _api_snapshot():
    with _snapshot_lock:
        data = _latest_snapshot
    if data is None:
        abort(503)
    return Response(data, mimetype="image/jpeg")


@_flask_app.route("/api/status")
def _api_status():
    m = _manager_ref
    stream = m.current_stream if m else None
    return jsonify({"active_stream": stream, "streaming": stream is not None})


def _start_webserver():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    _flask_app.run(host="0.0.0.0", port=8099, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _quote_uri(uri):
    return '"' + uri.replace('"', '\\"') + '"'


def _build_pipeline_string(stream: dict, hwaccel: str, cfg: dict) -> str:
    parser = "h264parse config-interval=1"
    caps = "video/x-h264,profile=high"
    mux = "flvmux"

    codec = stream.get("stream_codec", "h264").lower()

    if hwaccel == "nvenc":
        encoder = (
            f"nvh264enc name=enc bitrate={cfg['video_bitrate_kbps']} "
            f"gop-size={cfg['output_framerate']} preset=p5 repeat-sequence-header=true"
        )
        depay, parser_in, decoder = (
            ("rtph265depay", "h265parse config-interval=1", "nvh265dec")
            if codec == "h265" else
            ("rtph264depay", "h264parse config-interval=1", "nvh264dec")
        )
    else:
        encoder = (
            f"vah264enc name=enc bitrate={cfg['video_bitrate_kbps']} "
            f"key-int-max={cfg['output_framerate']} rate-control=vbr"
        )
        depay, parser_in, decoder = (
            ("rtph265depay", "h265parse config-interval=1", "vah265dec")
            if codec == "h265" else
            ("rtph264depay", "h264parse config-interval=1", "vah264dec")
        )

    rotation = stream.get("stream_rotation", 0)
    flip = {
        90:  "videoflip method=clockwise ! ",
        180: "videoflip method=rotate-180 ! ",
        270: "videoflip method=counterclockwise ! ",
    }.get(rotation, "")

    w, h, fr = cfg["output_width"], cfg["output_height"], cfg["output_framerate"]

    parts = [
        f"rtspsrc name=src0 location={_quote_uri(stream['stream_url'])} "
        f"protocols=tcp tcp-timeout=30000000 latency={cfg['rtsp_latency_ms']} ! "
        "queue name=qsrc0 ! "
        f"{depay} name=depay0 ! {parser_in} name=parse0 ! "
        "queue name=preq0 ! "
        f"{decoder} name=dec0 ! "
        f"videoconvert ! {flip}tee name=t ! "
        f"videoscale ! videorate ! "
        f"video/x-raw,width={w},height={h},framerate={fr}/1 ! "
        "queue name=postq0 ! "
        f"{encoder} ! "
        f"{caps} ! {parser} ! {mux} streamable=true name=mux ! "
        f"rtmpsink location={cfg['rtmp_url']}",

        "t. ! queue max-size-buffers=2 leaky=downstream ! "
        "videoscale ! video/x-raw,width=640,height=360 ! "
        "jpegenc quality=85 ! "
        "appsink name=snapsink emit-signals=false max-buffers=1 drop=true",

        "audiotestsrc is-live=true wave=silence ! "
        "audio/x-raw,channels=2,rate=44100 ! "
        "audioconvert ! audioresample ! "
        "voaacenc bitrate=128000 ! aacparse ! mux.",
    ]
    return " ".join(parts)


def _snapshot_loop(pipeline, snapshot_queue):
    """Runs as a daemon thread inside the worker process."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    snapsink = pipeline.get_by_name("snapsink")
    if snapsink is None:
        return

    while True:
        time.sleep(2)
        sample = snapsink.emit("try-pull-sample", 0)
        if sample is None:
            continue
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(info.data)
            buf.unmap(info)
            while True:
                try:
                    snapshot_queue.get_nowait()
                except Exception:
                    break
            try:
                snapshot_queue.put_nowait(data)
            except Exception:
                pass


def pipeline_worker(stream: dict, cfg: dict, snapshot_queue):
    """Runs in a child process. Owns GStreamer entirely."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib

    Gst.init(None)

    hwaccel = os.environ.get("RTSP_HWACCEL", "vaapi")
    stream_name = stream["stream_name"]

    pipeline_str = _build_pipeline_string(stream, hwaccel, cfg)
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
    startup_timeout = cfg.get("startup_output_timeout_sec", 20)
    stall_timeout = cfg.get("output_stall_timeout_sec", 10)

    enc = pipeline.get_by_name("enc")
    if enc:
        pad = enc.get_static_pad("src")
        if pad:
            def on_output_probe(_pad, _info):
                last_output_time[0] = time.monotonic()
                return Gst.PadProbeReturn.OK
            pad.add_probe(Gst.PadProbeType.BUFFER, on_output_probe)

    def check_stalled():
        now = time.monotonic()
        last = last_output_time[0]
        if last is None:
            if now - started_at >= startup_timeout:
                print(f"[worker] No output for {startup_timeout}s after start", flush=True)
                exit_code[0] = 1
                pipeline.set_state(Gst.State.NULL)
                loop.quit()
                return False
        elif now - last >= stall_timeout:
            print(f"[worker] Output stalled for {stall_timeout}s", flush=True)
            exit_code[0] = 1
            pipeline.set_state(Gst.State.NULL)
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(1, check_stalled)

    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_bus_message(_bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"[worker] Error: {err} ({dbg})", flush=True)
            exit_code[0] = 1
            pipeline.set_state(Gst.State.NULL)
            loop.quit()
        elif t == Gst.MessageType.WARNING:
            warn, _ = message.parse_warning()
            print(f"[worker] Warning: {warn}", flush=True)
        elif t == Gst.MessageType.EOS:
            print("[worker] EOS received", flush=True)
            exit_code[0] = 1
            pipeline.set_state(Gst.State.NULL)
            loop.quit()
        elif t == Gst.MessageType.STATE_CHANGED:
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

    threading.Thread(target=_snapshot_loop, args=(pipeline, snapshot_queue), daemon=True).start()

    try:
        loop.run()
    finally:
        bus.remove_signal_watch()

    sys.exit(exit_code[0])


# ---------------------------------------------------------------------------
# Home Assistant listener
# ---------------------------------------------------------------------------

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

    def _sync_entity(self):
        cfg = _get_cfg()
        stream_names = [s["stream_name"] for s in cfg.get("streams", [])]
        try:
            self._client.trigger_service(
                "input_select", "set_options",
                entity_id=cfg["ha_entity_id"],
                options=stream_names,
            )
            print(f"[ha] Set options on {cfg['ha_entity_id']}: {stream_names}", flush=True)
        except Exception as exc:
            print(f"[ha] Could not set options on {cfg['ha_entity_id']}: {exc}", flush=True)

    def run(self):
        cfg = _get_cfg()
        print(f"[ha] Connecting to {cfg['ha_url']}, entity={cfg['ha_entity_id']}", flush=True)
        self._client = WebsocketClient(cfg["ha_url"], cfg["ha_token"])
        while not self._stop.is_set():
            try:
                cfg = _get_cfg()
                if hasattr(self._client, "__enter__"):
                    self._client.__enter__()
                self._sync_entity()
                try:
                    state_obj = self._client.get_state(entity_id=cfg["ha_entity_id"])
                    state = getattr(state_obj, "state", None)
                    print(f"[ha] Connected. Initial state: {state!r}", flush=True)
                    if state:
                        self._on_state(state)
                except Exception as exc:
                    print(f"[ha] Initial state error: {exc}", flush=True)
                with self._client.listen_events("state_changed") as events:
                    for event in events:
                        if self._stop.is_set():
                            break
                        cfg = _get_cfg()
                        data = getattr(event, "data", None)
                        if isinstance(data, dict):
                            entity_id = data.get("entity_id")
                            new_state = data.get("new_state") or {}
                            state = new_state.get("state")
                        else:
                            entity_id = getattr(data, "entity_id", None)
                            new_state = getattr(data, "new_state", None)
                            state = getattr(new_state, "state", None) if new_state else None
                        if not entity_id or entity_id != cfg["ha_entity_id"]:
                            continue
                        print(f"[ha] State change: {entity_id} -> {state!r}", flush=True)
                        if state:
                            self._on_state(state)
            except Exception as exc:
                if self._stop.is_set():
                    break
                print(f"[ha] Websocket error: {exc}", flush=True)
                time.sleep(2)
            finally:
                if hasattr(self._client, "__exit__"):
                    try:
                        self._client.__exit__(None, None, None)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Pipeline manager
# ---------------------------------------------------------------------------

class PipelineManager(threading.Thread):
    def __init__(self, snapshot_queue):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._current_stream = None
        self._process = None
        self._stopping = threading.Event()
        self._snapshot_queue = snapshot_queue

    @property
    def current_stream(self):
        with self._lock:
            return self._current_stream

    def switch_stream(self, name):
        cfg = _get_cfg()
        stream_names = [s["stream_name"] for s in cfg.get("streams", [])]
        if name not in stream_names:
            print(f"[manager] Unknown stream: {name!r} (known: {stream_names})", flush=True)
            return
        with self._lock:
            if self._current_stream == name:
                return
            self._current_stream = name
        print(f"[manager] Switching to {name!r}", flush=True)
        self._terminate_current()

    def restart(self):
        print("[manager] Restarting pipeline due to config change", flush=True)
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
                stream_name = self._current_stream
            if stream_name is None:
                self._stopping.wait(0.5)
                continue

            cfg = _get_cfg()
            stream = next(
                (s for s in cfg.get("streams", []) if s["stream_name"] == stream_name),
                None,
            )
            if stream is None:
                streams = cfg.get("streams", [])
                if streams:
                    with self._lock:
                        self._current_stream = streams[0]["stream_name"]
                else:
                    self._stopping.wait(2)
                continue

            p = multiprocessing.Process(
                target=pipeline_worker,
                args=(stream, cfg, self._snapshot_queue),
                name=f"pipeline-{stream_name}",
            )
            p.start()
            print(f"[manager] Started pipeline process {p.pid} for {stream_name!r}", flush=True)
            with self._lock:
                self._process = p

            p.join()

            if self._stopping.is_set():
                break

            reconnect_delay = _get_cfg().get("reconnect_delay_sec", 2)
            if p.exitcode == -signal.SIGTERM:
                print(f"[manager] Pipeline {p.pid} terminated for stream switch", flush=True)
            else:
                print(
                    f"[manager] Pipeline {p.pid} exited (code {p.exitcode}),"
                    f" restarting in {reconnect_delay}s",
                    flush=True,
                )
                self._stopping.wait(reconnect_delay)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _load_config()

    snapshot_queue = multiprocessing.Queue(maxsize=2)

    def _drain_snapshots():
        global _latest_snapshot
        while True:
            try:
                data = snapshot_queue.get(timeout=5)
                with _snapshot_lock:
                    _latest_snapshot = data
            except Exception:
                pass

    threading.Thread(target=_drain_snapshots, daemon=True).start()

    global _manager_ref
    manager = PipelineManager(snapshot_queue)
    _manager_ref = manager
    manager.start()

    threading.Thread(target=_start_webserver, daemon=True).start()

    cfg = _get_cfg()
    ha_listener = None
    if cfg.get("ha_url") and cfg.get("ha_token"):
        ha_listener = HomeAssistantListener(manager.switch_stream)
        ha_listener.start()

    streams = cfg.get("streams", [])
    if streams:
        manager.switch_stream(streams[0]["stream_name"])

    stop_event = threading.Event()

    def _handle_signal(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    stop_event.wait()

    print("[main] Shutting down", flush=True)
    manager.stop()
    if ha_listener:
        ha_listener.stop()


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
