// Web UI for the addon. Bundled with React by esbuild at image build time (npm run build -> dist/app.js);
// served by rtsp_switcher.py together with index.html.
import React, { useState, useEffect, useCallback } from 'react';
import { createRoot } from 'react-dom/client';
const BASE = window.INGRESS_PATH || '';

// ── Addon connection tracking ─────────────────────────────────────────────────
// Every call to the addon goes through apiFetch, so the UI knows when it has lost contact
// instead of silently showing its last state as if it were current.
const conn = { failures: 0, lastOkAt: Date.now(), listeners: new Set() };
function noteConn(ok) {
  // A hidden page's requests can be throttled or cut off by the browser: not a lost addon
  if (!ok && document.hidden) return;
  const wasLost = conn.failures >= 2;
  if (ok) { conn.failures = 0; conn.lastOkAt = Date.now(); } else conn.failures++;
  if (wasLost !== (conn.failures >= 2)) conn.listeners.forEach(fn => fn());
}
async function apiFetch(path, opts) {
  let r;
  try { r = await fetch(`${BASE}${path}`, opts); }
  catch (e) { noteConn(false); throw e; }
  // 502/504 come from HA's ingress proxy when the addon isn't answering
  noteConn(r.status !== 502 && r.status !== 504);
  return r;
}
function useConnectionLost() {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    conn.listeners.add(fn);
    return () => conn.listeners.delete(fn);
  }, []);
  return conn.failures >= 2;
}
// ── Page visibility ───────────────────────────────────────────────────────────
// Polling pauses while the page is hidden (background tab, screen off, app minimised). On return,
// every poll runs at once and the page is shown as "last known state" until all have answered.
const pageVis = { hidden: document.hidden, resuming: new Set(), listeners: new Set() };
const notifyPage = () => pageVis.listeners.forEach(fn => fn());
document.addEventListener('visibilitychange', () => { pageVis.hidden = document.hidden; notifyPage(); });
function usePageState() {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    pageVis.listeners.add(fn);
    return () => pageVis.listeners.delete(fn);
  }, []);
  return { visible: !pageVis.hidden, resuming: pageVis.resuming.size > 0 };
}
// fn returns a promise resolving to true when it got fresh data. Runs now and every ms while the
// page is visible and enabled is true.
function usePoll(name, fn, ms, enabled = true) {
  const { visible } = usePageState();
  const started = React.useRef(false);
  useEffect(() => {
    if (!enabled || !visible) return;
    const resume = started.current;
    started.current = true;
    if (resume) { pageVis.resuming.add(name); notifyPage(); }
    const run = () => Promise.resolve(fn()).then(ok => { if (ok && pageVis.resuming.delete(name)) notifyPage(); });
    run();
    const id = setInterval(run, ms);
    return () => { clearInterval(id); if (pageVis.resuming.delete(name)) notifyPage(); };
  }, [name, fn, ms, enabled, visible]);
}

function ConnectionBanner() {
  const [, tick] = useState(0);
  useEffect(() => { const id = setInterval(() => tick(n => n + 1), 1000); return () => clearInterval(id); }, []);
  return (
    <div className="conn-banner">
      Lost connection to the addon &mdash; retrying&hellip; Showing last known state from {fmtDuration((Date.now() - conn.lastOkAt) / 1000)} ago.
    </div>
  );
}

// ── Icons ─────────────────────────────────────────────────────────────────────
const IconCamera = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>
    <circle cx="12" cy="13" r="3"/>
  </svg>
);
const IconSettings = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="3"/>
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
  </svg>
);
const IconEdit = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 20h9"/>
    <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>
  </svg>
);
const IconYouTube = () => (
  <svg width="24" height="22" viewBox="0 0 24 24">
    <path fill="#FF0000" d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.6 12 3.6 12 3.6s-7.5 0-9.4.5A3 3 0 0 0 .5 6.2 31.6 31.6 0 0 0 0 12a31.6 31.6 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1C4.5 20.4 12 20.4 12 20.4s7.5 0 9.4-.5a3 3 0 0 0 2.1-2.1A31.6 31.6 0 0 0 24 12a31.6 31.6 0 0 0-.5-5.8z"/>
    <polygon fill="#fff" points="9.6,15.6 15.8,12 9.6,8.4"/>
  </svg>
);

// ── Time formatting ───────────────────────────────────────────────────────────
function fmtTime(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d)) return null;
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function fmtElapsed(iso) {
  if (!iso) return null;
  return fmtDuration((Date.now() - new Date(iso)) / 1000);
}
function fmtDuration(sec) {
  const diff = Math.max(0, Math.floor(sec));
  if (diff < 60) return `${diff}s`;
  const s = diff % 60;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ${s}s`;
  const h = Math.floor(diff / 3600), m = Math.floor((diff % 3600) / 60);
  if (diff < 86400) return `${h}h ${m}m ${s}s`;
  const d = Math.floor(diff / 86400), rh = Math.floor((diff % 86400) / 3600), rm = Math.floor((diff % 3600) / 60);
  return `${d}d ${rh}h ${rm}m ${s}s`;
}

// ── YouTube status strip ──────────────────────────────────────────────────────
function HealthWarn({ health, issues }) {
  if (!health || health === 'good' || health === 'ok') return null;
  const isErr = health === 'bad' || health === 'noData';
  const cls = isErr ? 'yt-health-err' : 'yt-health-warn';
  const label = health === 'noData' ? 'No stream data' : `Stream ${health}`;
  const errorCount = (issues || []).filter(i => i.severity === 'error').length;
  const warnCount  = (issues || []).filter(i => i.severity === 'warning').length;
  const detail = [errorCount && `${errorCount} error${errorCount > 1 ? 's' : ''}`, warnCount && `${warnCount} warning${warnCount > 1 ? 's' : ''}`].filter(Boolean).join(', ');
  return (
    <span className={cls}>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2L1 21h22L12 2zm0 3.5L20.5 19h-17L12 5.5zM11 10v4h2v-4h-2zm0 6v2h2v-2h-2z"/></svg>
      {label}{detail ? ` · ${detail}` : ''}
    </span>
  );
}

// Past 2.5 poll intervals without a successful check we can't vouch for the YouTube status
const ytStaleSec = (yt) => 2.5 * (yt?.poll_interval_sec || 60);
function ytAgeSec(yt) {
  if (!yt?.checked_at) return null;
  return Math.floor((Date.now() - new Date(yt.checked_at)) / 1000);
}
// The one place the YouTube status is interpreted — the strip and the YouTube tab both render this,
// so they can never disagree. notes: { id, kind: 'info' | 'err', text }
function ytView(yt) {
  if (!yt?.configured) return { configured: false, badge: { cls: 'idle', label: 'Not set up' }, stripCls: 's-idle', notes: [], showHealth: false };
  const age = ytAgeSec(yt);
  if ((age === null || age > ytStaleSec(yt)) && !yt.starting) {
    return {
      configured: true, stale: true, badge: { cls: 'idle', label: 'Unknown' }, stripCls: 's-warn', showHealth: false,
      notes: [{ id: 'stale', kind: 'info', text: age === null ? 'YouTube status not checked yet' : `YouTube status last checked ${fmtElapsed(yt.checked_at)} ago` }],
    };
  }
  const live = yt.live;
  const notSending = (live || yt.starting) && !yt.rtmp_sending;
  const health = yt.stream_health;
  const hasError = notSending || (live && (health === 'bad' || health === 'noData'));
  const hasWarn  = !hasError && (yt.starting || (live && health && health !== 'good' && health !== 'ok'));
  const notes = [];
  if (live && yt.started_at) notes.push({ id: 'started', kind: 'info', text: `Stream started ${fmtElapsed(yt.started_at)} ago` });
  if (yt.starting && !notSending) notes.push({ id: 'starting', kind: 'info', text: 'Waiting for YouTube to confirm' });
  if (notSending) notes.push({ id: 'not-sending', kind: 'err', text: 'Not sending video to YouTube' });
  return {
    configured: true, stale: false,
    badge: yt.starting ? { cls: 'idle', label: 'Starting…' } : live ? { cls: 'live', label: 'Live' } : { cls: 'idle', label: 'Idle' },
    stripCls: !live && !yt.starting ? 's-idle' : hasError ? 's-error' : hasWarn ? 's-warn' : 's-live',
    notes, showHealth: live && !notSending,
  };
}

// onSetup: opens the YouTube tab (omitted on that tab and in the dashboard embed, which has no tabs)
function YtStrip({ yt, onSetup }) {
  const [, tick] = useState(0);
  useEffect(() => { const id = setInterval(() => tick(n => n + 1), 1000); return () => clearInterval(id); }, []);
  const v = ytView(yt);
  if (!yt) return <div className="yt-strip s-idle" />; // not loaded yet: say nothing rather than guess
  if (!v.configured) return (
    <div className="yt-strip s-idle">
      <span className="yt-strip-note">YouTube not set up</span>
      {onSetup && <button type="button" className="btn btn-ghost btn-small" onClick={onSetup}>Set up</button>}
    </div>
  );
  return (
    <div className={`yt-strip ${v.stripCls}`}>
      <span className={`yt-status-badge ${v.badge.cls}`}>{v.badge.label}</span>
      {v.notes.map(n => <span key={n.id} className={n.kind === 'err' ? 'yt-health-err' : 'yt-strip-time'}>{n.text}</span>)}
      {v.showHealth && <HealthWarn health={yt.stream_health} issues={yt.stream_issues} />}
    </div>
  );
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function Toast({ toast }) {
  if (!toast) return null;
  return <div className={`toast ${toast.ok ? 'toast-ok' : 'toast-err'}`}>{toast.msg}</div>;
}

// ── Snapshot ──────────────────────────────────────────────────────────────────
// Frames are captured every 2s and the status is fetched every 3s, so a healthy feed is at most ~5s old
const SNAP_FRESH_SEC = 6;
const SNAP_NO_SIGNAL_SEC = 10;

// snap: { stream, id, age_sec } from /api/status (age as of receivedAt), or null
function Snapshot({ snap, camera, receivedAt }) {
  const [, tick] = useState(0);
  useEffect(() => { const id = setInterval(() => tick(n => n + 1), 1000); return () => clearInterval(id); }, []);
  const [failedId, setFailedId] = useState(null);
  // Never show a frame from a camera other than the selected one
  const frame = snap && camera && snap.stream === camera ? snap : null;
  const shown = frame && failedId !== frame.id;
  const { resuming } = usePageState();
  const age = frame ? frame.age_sec + (Date.now() - receivedAt) / 1000 : null;
  // While resuming, receivedAt is from before the page was hidden, so the age would be wrong
  const noSignal = shown && !resuming && age > SNAP_NO_SIGNAL_SEC;
  return (
    <div className="snapshot-outer">
      {frame && (
        <img
          key={camera}
          src={`${BASE}/api/snapshot?t=${frame.id}`}
          className={noSignal ? 'snap-dim' : ''}
          style={{ display: shown ? 'block' : 'none' }}
          onError={() => setFailedId(frame.id)}
          alt=""
        />
      )}
      {!shown && (
        <div className="snapshot-loading waiting">
          {camera && <div className="snapshot-spinner" />}
          <span>{camera ? `Waiting for video from ${camera}…` : 'No camera selected'}</span>
        </div>
      )}
      {noSignal && <div className="snap-overlay">No signal from {camera} &mdash; last frame {fmtDuration(age)} ago</div>}
      {shown && !noSignal && !resuming && age > SNAP_FRESH_SEC && <div className="snap-age">updated {fmtDuration(age)} ago</div>}
    </div>
  );
}

// ── VideoPreview (WebCodecs + WebSocket, falls back to Snapshot) ──────────────
function parseSpsCodecString(bytes) {
  for (let i = 0; i < bytes.length - 8; i++) {
    if (bytes[i]===0 && bytes[i+1]===0 && bytes[i+2]===0 && bytes[i+3]===1) {
      if ((bytes[i+4] & 0x1f) === 7) { // NAL type 7 = SPS
        const p = bytes[i+5].toString(16).padStart(2,'0');
        const c = bytes[i+6].toString(16).padStart(2,'0');
        const l = bytes[i+7].toString(16).padStart(2,'0');
        return `avc1.${p}${c}${l}`;
      }
    }
  }
  return 'avc1.42E01E'; // fallback
}

// Frames carry their camera name, so the overlay follows what is actually decoded rather than
// the 3s status poll: "Waiting for video from X…" until X's frames decode, dimmed "No signal" if they stop.
// Mirrors _preview_available() on the server: the preview branch is only built for H.264 cameras
function previewAvailable(config, camera) {
  const st = (config?.streams || []).find(s => s.stream_name === camera);
  return !st || (st.stream_codec || 'h264').toLowerCase() === 'h264';
}

function VideoPreview({ snap, camera, receivedAt }) {
  const canvasRef = React.useRef(null);
  // mode: 'connecting' | 'video' | 'fallback'
  const [mode, setMode] = React.useState('connecting');
  const [statusMsg, setStatusMsg] = React.useState('Connecting…');
  const [decodedCamera, setDecodedCamera] = React.useState(null); // camera of the frame on the canvas
  const lastFrameAtRef = React.useRef(0);
  const [, tick] = useState(0);
  useEffect(() => { const id = setInterval(() => tick(n => n + 1), 1000); return () => clearInterval(id); }, []);

  const supportsWebCodecs = typeof VideoDecoder !== 'undefined';

  useEffect(() => {
    if (!supportsWebCodecs) {
      const reason = !window.isSecureContext
        ? 'WebCodecs requires HTTPS or localhost — current page is not a secure context'
        : 'VideoDecoder API not available in this browser';
      console.warn('[VideoPreview]', reason);
      setMode('fallback');
      setStatusMsg(reason);
      return;
    }

    const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProto}//${location.host}${BASE}/ws/video`;
    console.log('[VideoPreview] connecting to', wsUrl);

    let closed = false;
    let decoder = null;
    let needsReset = true;
    let decoderGen = null;  // pipeline generation the decoder was configured for
    let frameCamera = null; // camera of the frames being fed to the decoder
    let localTs = 0; // monotonic fallback timestamp (µs)
    const textDecoder = new TextDecoder();

    function makeDecoder(codecStr) {
      if (decoder && decoder.state !== 'closed') { try { decoder.close(); } catch(_) {} }
      console.log('[VideoPreview] configuring decoder, codec:', codecStr);
      decoder = new VideoDecoder({
        output: (frame) => {
          const canvas = canvasRef.current;
          if (canvas) canvas.getContext('2d').drawImage(frame, 0, 0, canvas.width, canvas.height);
          frame.close();
          lastFrameAtRef.current = Date.now();
          setDecodedCamera(frameCamera);
          setMode('video');
        },
        error: (e) => {
          console.warn('[VideoPreview] decoder error:', e);
          needsReset = true;
        },
      });
      decoder.configure({ codec: codecStr, optimizeForLatency: true });
    }

    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      console.log('[VideoPreview] WebSocket open');
      needsReset = true;
      setStatusMsg('Waiting for keyframe…');
    };

    ws.onmessage = (evt) => {
      if (!(evt.data instanceof ArrayBuffer) || evt.data.byteLength < 15) return;
      const view = new DataView(evt.data);
      const isKeyframe = view.getUint8(0) === 1;
      // Use GStreamer PTS if non-zero, otherwise a local monotonic counter
      const ptsUs = Number(view.getBigUint64(1, false));
      const timestamp = ptsUs > 0 ? ptsUs : localTs;
      localTs += 33333; // ~30fps fallback increment
      const gen = view.getUint32(9, false);
      const nameLen = view.getUint16(13, false);
      if (evt.data.byteLength < 15 + nameLen) return;
      const name = textDecoder.decode(new Uint8Array(evt.data, 15, nameLen));
      const h264 = new Uint8Array(evt.data, 15 + nameLen);

      // New pipeline (camera switch or restart) → start a fresh decoder at its first keyframe
      if (gen !== decoderGen) needsReset = true;
      if (needsReset) {
        if (!isKeyframe) return; // wait for IDR before decoding anything
        needsReset = false;
        decoderGen = gen;
        makeDecoder(parseSpsCodecString(h264));
        // fall through and decode this IDR frame immediately
      }
      frameCamera = name; // same generation can be renamed without a restart

      if (!decoder || decoder.state !== 'configured') return;
      try {
        decoder.decode(new EncodedVideoChunk({ type: isKeyframe ? 'key' : 'delta', timestamp, data: h264 }));
      } catch(e) {
        console.warn('[VideoPreview] decode error:', e);
        needsReset = true;
      }
    };

    ws.onerror = (e) => {
      console.warn('[VideoPreview] WebSocket error', e);
      if (!closed) { setMode('fallback'); setStatusMsg('WebSocket error — showing snapshots'); }
    };
    ws.onclose = (e) => {
      console.log('[VideoPreview] WebSocket closed, code:', e.code, e.reason);
      if (!closed) { setMode('fallback'); setStatusMsg(`WebSocket closed (${e.code}) — showing snapshots`); }
    };

    return () => {
      closed = true;
      ws.close();
      if (decoder && decoder.state !== 'closed') { try { decoder.close(); } catch(_) {} }
    };
  }, []);

  if (mode === 'fallback') {
    return (
      <div>
        <Snapshot snap={snap} camera={camera} receivedAt={receivedAt} />
        <div className="preview-status">{statusMsg}</div>
      </div>
    );
  }
  // Never show another camera's video under this camera's name
  const shown = mode === 'video' && camera && decodedCamera === camera;
  const age = shown ? (Date.now() - lastFrameAtRef.current) / 1000 : null;
  const noSignal = shown && age > SNAP_NO_SIGNAL_SEC;
  return (
    <div>
      <div className="snapshot-outer">
        <canvas ref={canvasRef} width={1920} height={1080} className={noSignal ? 'snap-dim' : ''}
          style={{ display: shown ? 'block' : 'none' }} />
        {!shown && (
          <div className="snapshot-loading waiting">
            {camera && <div className="snapshot-spinner" />}
            <span>{camera ? `Waiting for video from ${camera}…` : 'No camera selected'}</span>
          </div>
        )}
        {noSignal && <div className="snap-overlay">No signal from {camera} &mdash; last frame {fmtDuration(age)} ago</div>}
      </div>
      <div className={`preview-status${shown && !noSignal ? ' live' : ''}`}>
        {shown && !noSignal ? '● Live video' : mode === 'connecting' ? statusMsg : 'Live preview'}
      </div>
    </div>
  );
}

// ── Camera modal ──────────────────────────────────────────────────────────────
function CameraModal({ initial, onAir, onSave, onDelete, onClose }) {
  const isEdit = !!initial;
  const [name, setName] = useState(initial?.stream_name || '');
  const [url, setUrl] = useState(initial?.stream_url || '');
  const [detected, setDetected] = useState(
    initial ? { width: initial.stream_width, height: initial.stream_height, framerate: initial.stream_framerate, codec: initial.stream_codec } : null
  );
  const [detecting, setDetecting] = useState(false);
  const [detectError, setDetectError] = useState(null);
  const dialogRef = React.useRef(null);
  // Focus the first field on open; give focus back to whatever opened the dialog on close
  useEffect(() => {
    const opener = document.activeElement;
    dialogRef.current?.querySelector('input')?.focus();
    return () => { if (opener && document.contains(opener)) opener.focus(); };
  }, []);
  // Escape closes; Tab cycles within the dialog instead of reaching the page behind it
  const onDialogKey = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); onClose(); return; }
    if (e.key !== 'Tab') return;
    const items = [...dialogRef.current.querySelectorAll('input, select, button')].filter(el => !el.disabled);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };

  const probe = async (probeUrl) => {
    const u = (probeUrl ?? url).trim();
    if (!u) { setDetectError('Enter an RTSP URL first'); return null; }
    setDetecting(true);
    setDetectError(null);
    setDetected(null);
    try {
      const r = await apiFetch(`/api/probe`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: u }) });
      const d = await r.json();
      if (d.error) { setDetectError(d.error); setDetecting(false); return null; }
      setDetected(d);
      setDetecting(false);
      return d;
    } catch { setDetectError('Could not reach server'); setDetecting(false); return null; }
  };

  const handleSave = async () => {
    if (!name.trim() || !url.trim()) return;
    let det = detected;
    if (!det) {
      det = await probe(url);
      if (!det) return;
    }
    const form = { stream_name: name.trim(), stream_url: url.trim(), stream_width: det.width, stream_height: det.height, stream_framerate: det.framerate, stream_codec: det.codec };
    const videoChanged = isEdit && ['stream_url', 'stream_width', 'stream_height', 'stream_framerate', 'stream_codec'].some(k => form[k] !== initial[k]);
    if (onAir && videoChanged && !confirm(`${initial.stream_name} is on air — saving will interrupt the YouTube stream for a few seconds. Continue?`)) return;
    onSave(form);
  };

  const codecLabel = detected ? (detected.codec === 'h265' ? 'H.265' : 'H.264') : null;

  return (
    <div className="modal-bg" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="camera-modal-title" onKeyDown={onDialogKey}>
        <div className="modal-title" id="camera-modal-title">{isEdit ? 'Edit Camera' : 'Add Camera'}</div>
        <div className="form-grid">
          <div className="field field-full">
            <label htmlFor="f-name">Name</label>
            <input id="f-name" value={name} onChange={e => setName(e.target.value)} placeholder="Inside Box" />
          </div>
          <div className="field field-full">
            <label htmlFor="f-rtsp-url">RTSP URL</label>
            <div style={{ display: 'flex', gap: 8 }}>
              <input id="f-rtsp-url" value={url} onChange={e => { setUrl(e.target.value); setDetected(null); setDetectError(null); }} placeholder="rtsp://192.168.1.x:8554/stream" style={{ flex: 1 }} />
              {isEdit && (
                <button className="btn btn-ghost" onClick={() => probe(url)} disabled={detecting} title="Re-detect stream info" style={{ flexShrink: 0 }}>
                  {detecting ? '…' : '↻'}
                </button>
              )}
            </div>
          </div>
        </div>
        <div style={{ marginTop: 14, minHeight: 32, display: 'flex', alignItems: 'center' }}>
          {detecting && <span style={{ fontSize: 13, color: 'var(--muted)' }}>Detecting stream…</span>}
          {!detecting && detected && (
            <span style={{ fontSize: 13, color: 'var(--success)' }}>
              {detected.width}×{detected.height} · {codecLabel} · {detected.framerate}fps
            </span>
          )}
          {!detecting && detectError && <span style={{ fontSize: 13, color: 'var(--danger)' }}>{detectError}</span>}
          {!detecting && !detected && !detectError && isEdit && <span style={{ fontSize: 13, color: 'var(--muted)' }}>Press ↻ to re-detect</span>}
          {!detecting && !detected && !detectError && !isEdit && <span style={{ fontSize: 13, color: 'var(--muted)' }}>Stream will be detected on save</span>}
        </div>
        <div className="modal-footer">
          {onDelete && (
            <button className={`btn btn-danger${onAir ? ' is-disabled' : ''}`} style={{ marginRight: 'auto' }}
              aria-disabled={onAir} title={onAir ? 'Switch to another camera first' : undefined} onClick={onDelete}>Delete</button>
          )}
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={handleSave} disabled={detecting}>Save</button>
        </div>
      </div>
    </div>
  );
}

// ── Camera tab ────────────────────────────────────────────────────────────────
function CameraTab({ config, onConfigChange, showToast }) {
  const [status, setStatus] = useState(null);
  const [statusAt, setStatusAt] = useState(0);
  const [modal, setModal] = useState(null);
  const [pending, setPending] = useState(null); // camera tapped, until the server reports it selected

  const refresh = useCallback(() => {
    return apiFetch(`/api/status`).then(r => r.json()).then(data => {
      setStatus(data);
      setStatusAt(Date.now());
      return true;
    }).catch(() => false);
  }, []);
  usePoll('status', refresh, 3000);
  const { visible } = usePageState();

  useEffect(() => {
    if (pending && status?.active_stream === pending) setPending(null);
  }, [status, pending]);

  if (!config) return null;
  const streams = config.streams || [];

  const switchTo = async (name) => {
    if (name === (pending || status?.active_stream)) return;
    setPending(name);
    try {
      const r = await apiFetch(`/api/switch`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.error || 'Switch failed');
      }
      refresh();
    } catch (e) {
      setPending(null);
      showToast(e.message === 'Failed to fetch' ? 'Could not reach the addon' : e.message, false);
    }
  };

  const persist = async (newStreams) => {
    const newCfg = { ...config, streams: newStreams };
    const res = await saveConfigDetailed(newCfg);
    if (res.ok) { onConfigChange(newCfg); showToast(res.restarted ? 'Saved — video restarted' : 'Saved', true); }
    else showToast(res.error || 'Save failed', false);
  };

  const handleSave = (form) => {
    const newStreams = modal === 'add' ? [...streams, form] : streams.map((s, i) => i === modal ? form : s);
    persist(newStreams);
    setModal(null);
  };

  const handleDelete = (idx) => {
    if (streams[idx].stream_name === active) { showToast('Switch to another camera first', false); return; }
    if (!confirm(`Delete "${streams[idx].stream_name}"?`)) return;
    persist(streams.filter((_, i) => i !== idx));
    setModal(null);
  };

  const active = status?.active_stream;
  const shownCamera = pending || active;
  const CAM_STATE = {
    switching: { label: 'Switching…' }, connecting: { label: 'Connecting…' },
    reconnecting: { label: 'Reconnecting…' }, no_signal: { label: 'No signal', bad: true },
  };
  const rowState = (name) => {
    if (name === pending) return 'switching';
    if (name === status?.on_air_stream) return 'on_air';
    if (!pending && name === active) return status?.selected_state;
    return null;
  };

  return (
    <div className="content">
      {!config?.video_preview
        ? <Snapshot snap={status?.snapshot} camera={shownCamera} receivedAt={statusAt} />
        : previewAvailable(config, shownCamera)
          ? (visible ? <VideoPreview snap={status?.snapshot} camera={shownCamera} receivedAt={statusAt} /> : null)
          : <div>
              <Snapshot snap={status?.snapshot} camera={shownCamera} receivedAt={statusAt} />
              <div className="preview-status">Live preview isn&rsquo;t available for {shownCamera}&rsquo;s video format (H.265) &mdash; showing snapshots</div>
            </div>
      }
      <div className="camera-section">
        <div className="camera-list">
          {streams.map((s, i) => {
            const state = rowState(s.stream_name);
            const rowCls = state === 'on_air' ? ' active' : s.stream_name === shownCamera ? ' selected' : '';
            return (
              <div key={i} className={`camera-row${rowCls}`} onClick={() => switchTo(s.stream_name)}
                role="button" tabIndex={0} aria-pressed={state === 'on_air'}
                aria-label={`${s.stream_name}${state === 'on_air' ? ', on air' : CAM_STATE[state] ? `, ${CAM_STATE[state].label.replace('…', '').toLowerCase()}` : ''}`}
                onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); switchTo(s.stream_name); } }}>
                <div className="cam-info">
                  <span className="camera-name">{s.stream_name}</span>
                  {state === 'on_air' && <span className="live-dot">On air</span>}
                  {CAM_STATE[state] && <span className={`cam-state${CAM_STATE[state].bad ? ' bad' : ''}`}>{CAM_STATE[state].label}</span>}
                </div>
                <div className="cam-actions">
                  <button className="btn btn-ghost btn-icon" aria-label={`Edit ${s.stream_name}`} title="Edit" onClick={e => { e.stopPropagation(); setModal(i); }}><IconEdit /></button>
                </div>
              </div>
            );
          })}
        </div>
        <button className="btn-add" onClick={() => setModal('add')}>+ Add Camera</button>
      </div>
      {modal !== null && (
        <CameraModal initial={modal === 'add' ? null : streams[modal]} onAir={modal !== 'add' && streams[modal]?.stream_name === active} onSave={handleSave} onDelete={modal === 'add' ? null : () => handleDelete(modal)} onClose={() => setModal(null)} />
      )}
    </div>
  );
}

// ── YouTube stream presets ────────────────────────────────────────────────────
const YT_RESOLUTIONS = [
  { label: '360p',    width: 640,  height: 360,  fps: 30 },
  { label: '480p',    width: 854,  height: 480,  fps: 30 },
  { label: '720p30',  width: 1280, height: 720,  fps: 30 },
  { label: '720p60',  width: 1280, height: 720,  fps: 60 },
  { label: '1080p30', width: 1920, height: 1080, fps: 30 },
  { label: '1080p60', width: 1920, height: 1080, fps: 60 },
  { label: '1440p30', width: 2560, height: 1440, fps: 30 },
  { label: '1440p60', width: 2560, height: 1440, fps: 60 },
  { label: '4K / 2160p30', width: 3840, height: 2160, fps: 30 },
  { label: '4K / 2160p60', width: 3840, height: 2160, fps: 60 },
];
const YT_BITRATES = [
  { label: '2 Mbps',  kbps: 2000  },
  { label: '3 Mbps',  kbps: 3000  },
  { label: '4 Mbps',  kbps: 4000  },
  { label: '6 Mbps',  kbps: 6000  },
  { label: '8 Mbps',  kbps: 8000  },
  { label: '10 Mbps', kbps: 10000 },
  { label: '12 Mbps', kbps: 12000 },
  { label: '15 Mbps', kbps: 15000 },
  { label: '20 Mbps', kbps: 20000 },
  { label: '25 Mbps', kbps: 25000 },
  { label: '30 Mbps', kbps: 30000 },
  { label: '35 Mbps', kbps: 35000 },
  { label: '50 Mbps', kbps: 50000 },
];
function findResolution(w, h, fps) {
  return YT_RESOLUTIONS.find(r => r.width === w && r.height === h && r.fps === fps) || null;
}
function findBitrate(kbps) {
  return YT_BITRATES.find(b => b.kbps === kbps) || null;
}

// ── Settings tab ──────────────────────────────────────────────────────────────
function SettingsTab({ config, onConfigChange, showToast, drafts, setDrafts, yt }) {
  const [errors, setErrors] = useState({});
  const [form, update, clearDraft] = draftFor(drafts, setDrafts, 'settings', pick(config, SETTINGS_FIELDS));
  const set = (k, v) => update(f => ({ ...f, [k]: v }));
  // Same limits the server enforces
  const LIMITS = {
    rtsp_latency_ms: [0, 5000], reconnect_delay_sec: [1, 60],
    output_stall_timeout_sec: [5, 120], startup_output_timeout_sec: [10, 300],
  };

  const handleSave = async () => {
    const out = { ...config, ...form }, errs = {};
    for (const [k, [lo, hi]] of Object.entries(LIMITS)) {
      const raw = String(form[k] ?? '').trim();
      const v = Number(raw);
      if (raw === '' || !Number.isInteger(v) || v < lo || v > hi) errs[k] = `Enter a whole number from ${lo} to ${hi}`;
      else out[k] = v;
    }
    setErrors(errs);
    if (Object.keys(errs).length) { showToast('Fix the highlighted settings', false); return; }
    if (!confirmVideoRestart(config, out, yt)) return;
    const res = await saveConfigDetailed(out);
    if (res.ok) { clearDraft(); onConfigChange(out); showToast(res.restarted ? 'Saved — video restarted' : 'Saved', true); }
    else {
      if (res.field) setErrors({ [res.field]: res.error });
      showToast(res.error || 'Save failed', false);
    }
  };
  const numField = (k, label) => (
    <div className="field">
      <label htmlFor={`f-${k}`}>{label}</label>
      <input id={`f-${k}`} type="number" value={form[k] ?? ''} onChange={e => set(k, e.target.value)}
        style={errors[k] ? { borderColor: 'var(--danger)' } : undefined} />
      <span style={{ fontSize: 12, color: errors[k] ? 'var(--danger)' : 'var(--muted)' }}>{errors[k] || `${LIMITS[k][0]}–${LIMITS[k][1]}`}</span>
    </div>
  );

  return (
    <div className="content">
      <div className="settings-content">
        <div className="card">
          <h2>Home Assistant</h2>
          <div className="form-grid">
            <div className="field field-full"><label htmlFor="f-websocket-url">WebSocket URL</label><input id="f-websocket-url" value={form.ha_url || ''} onChange={e => set('ha_url', e.target.value)} placeholder="ws://homeassistant:8123/api/websocket" /></div>
            <div className="field field-full"><label htmlFor="f-long-lived-access-token">Long-Lived Access Token</label><input id="f-long-lived-access-token" type="password" value={form.ha_token || ''} onChange={e => set('ha_token', e.target.value)} placeholder={secretSaved(config, 'ha_token') ? SECRET_PLACEHOLDER : ''} autoComplete="new-password" /></div>
            <div className="field field-full"><label htmlFor="f-entity-id">Entity ID</label><input id="f-entity-id" value={form.ha_entity_id || ''} onChange={e => set('ha_entity_id', e.target.value)} placeholder="input_select.camera_view" /></div>
          </div>
        </div>
        <div className="card">
          <h2>Advanced</h2>
          <div className="form-grid">
            {numField('rtsp_latency_ms', 'RTSP Latency (ms)')}
            {numField('reconnect_delay_sec', 'Reconnect Delay (s)')}
            {numField('output_stall_timeout_sec', 'Output Stall Timeout (s)')}
            {numField('startup_output_timeout_sec', 'Startup Output Timeout (s)')}
          </div>
          <label className="toggle-row" style={{ marginTop: 14 }}>
            <span style={{ fontSize: 14, color: 'var(--text)' }}>Live video preview — experimental. Saving this restarts the video for a few seconds.</span>
            <span className="toggle">
              <input type="checkbox" checked={!!form.video_preview} onChange={e => set('video_preview', e.target.checked)} />
              <span className="toggle-slider" />
            </span>
          </label>
        </div>
        <button className="btn btn-primary" onClick={handleSave}>Save Settings</button>
      </div>
    </div>
  );
}

// ── YouTube tab ───────────────────────────────────────────────────────────────
function YouTubeTab({ config, onConfigChange, showToast, yt, setYt, drafts, setDrafts }) {
  const [authFlow, setAuthFlow] = useState(null); // {user_code, verification_url}
  const [authUnreachable, setAuthUnreachable] = useState(false);
  const [polling, setPolling] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [creds, setCreds, clearCreds] = draftFor(drafts, setDrafts, 'ytCreds',
    { youtube_client_id: config.youtube_client_id || '', youtube_client_secret: config.youtube_client_secret || '' });
  const [streamForm, setStreamForm, clearStream] = draftFor(drafts, setDrafts, 'ytStream',
    { ...pick(config, STREAM_FIELDS), ...splitRtmp(config.rtmp_url) });
  const [showKey, setShowKey] = useState(false); // resets (re-masks) when the tab is left

  // While device flow is active, poll every 5s
  useEffect(() => {
    if (!authFlow) return;
    setAuthUnreachable(false);
    const id = setInterval(async () => {
      let d;
      try {
        const r = await apiFetch(`/api/youtube/auth/poll`, { method: 'POST' });
        d = await r.json();
      } catch {
        setAuthUnreachable(true);
        return;
      }
      setAuthUnreachable(false);
      if (d.ok) {
        setAuthFlow(null);
        showToast('YouTube authorised', true);
        apiFetch(`/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {});
      } else if (!d.pending) {
        setAuthFlow(null);
        showToast(d.error || 'Auth failed', false);
      }
    }, 5000);
    return () => clearInterval(id);
  }, [authFlow]);

  const saveCreds = async () => {
    setPolling(true);
    const newCfg = { ...config, ...creds };
    const ok = await saveConfig(newCfg);
    setPolling(false);
    if (ok) { clearCreds(); onConfigChange(newCfg); showToast('Saved', true); }
    else showToast('Save failed', false);
  };

  const startAuth = async () => {
    try {
      const r = await apiFetch(`/api/youtube/auth/start`, { method: 'POST' });
      const d = await r.json();
      if (d.error) { showToast(d.error, false); return; }
      setAuthFlow(d);
    } catch { showToast('Failed to start auth', false); }
  };

  const revokeAuth = async () => {
    if (!confirm('Remove stored refresh token?')) return;
    try {
      const r = await apiFetch(`/api/youtube/auth/revoke`, { method: 'POST' });
      if (!r.ok) throw new Error();
      showToast('Token removed', true);
    } catch { showToast('Failed to remove token', false); }
    apiFetch(`/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {});
  };

  const doRefresh = async () => {
    await apiFetch(`/api/youtube/poll`, { method: 'POST' });
    // give the background thread a moment to complete the poll
    setTimeout(() => apiFetch(`/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {}), 2000);
  };

  const doRestart = async () => {
    setRestarting(true);
    try {
      const r = await apiFetch(`/api/youtube/restart`, { method: 'POST' });
      const d = await r.json();
      showToast(d.message || (d.ok ? 'Done' : 'Failed'), d.ok);
    } catch { showToast('Request failed', false); }
    setRestarting(false);
    apiFetch(`/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {});
  };

  const toggleAutoRestart = async (e) => {
    const enabled = e.target.checked;
    try {
      const r = await apiFetch(`/api/youtube/auto_restart`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'Could not change auto-restart');
      setYt(s => ({ ...s, auto_restart: enabled }));
    } catch (err) {
      // The toggle stays bound to the server's value, so it shows the real setting
      showToast(err.message === 'Failed to fetch' ? 'Could not reach the addon' : err.message, false);
    }
  };

  const live = yt?.live;
  const configured = yt?.configured;
  const view = ytView(yt);
  const noVideo = yt?.rtmp_sending === false; // the addon's own pipeline state, not YouTube's, so it's current even when YouTube's status is stale

  // Configured values that match no preset get their own "Current: …" entry, so the dropdowns never
  // display a value that isn't what's actually configured
  const CURRENT = '__current';
  const cfgResCustom = config && !findResolution(config.output_width, config.output_height, config.output_framerate);
  const cfgBrCustom  = config && !findBitrate(config.video_bitrate_kbps);
  const resValue = streamForm ? (findResolution(streamForm.output_width, streamForm.output_height, streamForm.output_framerate)?.label ?? CURRENT) : CURRENT;
  const brValue  = streamForm ? (findBitrate(streamForm.video_bitrate_kbps)?.label ?? CURRENT) : CURRENT;
  const setResolution = (label) => {
    if (label === CURRENT) { setStreamForm(f => ({ ...f, output_width: config.output_width, output_height: config.output_height, output_framerate: config.output_framerate })); return; }
    const r = YT_RESOLUTIONS.find(x => x.label === label);
    if (r) setStreamForm(f => ({ ...f, output_width: r.width, output_height: r.height, output_framerate: r.fps }));
  };
  const setBitrate = (label) => {
    if (label === CURRENT) { setStreamForm(f => ({ ...f, video_bitrate_kbps: config.video_bitrate_kbps })); return; }
    const b = YT_BITRATES.find(x => x.label === label);
    if (b) setStreamForm(f => ({ ...f, video_bitrate_kbps: b.kbps }));
  };
  const saveStream = async () => {
    const { rtmp_server, rtmp_key, ...rest } = streamForm;
    const newCfg = { ...config, ...rest, rtmp_url: joinRtmp(rtmp_server, rtmp_key) };
    if (!confirmVideoRestart(config, newCfg, yt)) return;
    const res = await saveConfigDetailed(newCfg);
    if (res.ok) { clearStream(); onConfigChange(newCfg); showToast(res.restarted ? 'Saved — video restarted' : 'Saved', true); }
    else showToast(res.error || 'Save failed', false);
  };

  return (
    <div className="content">
      <div className="settings-content">

        <div className="card">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <h2 style={{ marginBottom: 0 }}>Broadcast Status</h2>
            <span className={`yt-status-badge ${view.badge.cls}`}>{view.badge.label}</span>
          </div>
          {/* Same state notes as the strip; 'started' is shown below with the clock time as well */}
          {view.notes.filter(n => n.id !== 'started').map(n => (
            <div key={n.id} style={{ fontSize: 13, fontWeight: n.kind === 'err' ? 600 : 400, color: n.kind === 'err' ? 'var(--danger)' : 'var(--muted)', marginBottom: 6 }}>{n.text}</div>
          ))}
          {configured && !view.stale && yt?.checked_at && <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>Checked with YouTube {fmtElapsed(yt.checked_at)} ago</div>}
          {yt?.title && <div style={{ fontSize: 14, color: 'var(--text)', marginBottom: 4 }}>{yt.title}</div>}
          {live && !view.stale && yt?.started_at && <div style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 4 }}>Started {fmtTime(yt.started_at)} &middot; {fmtElapsed(yt.started_at)}</div>}
          {!live && yt?.ended_at && <div style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 4 }}>Ended {fmtTime(yt.ended_at)}</div>}
          {!configured && <div style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 12 }}>Not authorised — complete OAuth setup below.</div>}
          {view.showHealth && yt?.stream_issues?.length > 0 && (
            <div className="issue-list">
              {yt.stream_issues.map((issue, i) => (
                <div key={i} className={`issue-row ${issue.severity || 'warning'}`}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" style={{ flexShrink: 0, marginTop: 1 }}><path d="M12 2L1 21h22L12 2zm0 3.5L20.5 19h-17L12 5.5zM11 10v4h2v-4h-2zm0 6v2h2v-2h-2z"/></svg>
                  <span className="issue-reason">{issue.reason || issue.type}</span>
                </div>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: yt?.last_error ? 10 : 0 }}>
            <button className="btn btn-primary" onClick={doRestart} disabled={restarting || !configured || noVideo}>
              {restarting ? 'Restarting…' : 'Restart Broadcast'}
            </button>
            <button className="btn btn-ghost" onClick={doRefresh}>Refresh</button>
          </div>
          {configured && noVideo && (
            <div style={{ fontSize: 13, color: 'var(--muted)', marginTop: 8 }}>
              {view.notes.some(n => n.id === 'not-sending') ? 'Restart is available once the camera is streaming.' : 'No video is being sent to YouTube — Restart is available once the camera is streaming.'}
            </div>
          )}
          {yt?.last_error && <div style={{ fontSize: 13, color: 'var(--danger)', marginTop: 8, wordBreak: 'break-word' }}>{yt.last_error}</div>}
          {configured && yt?.quota_budget > 0 && <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>API quota used today: ~{yt.quota_used} / {yt.quota_budget} units (estimate, resets midnight Pacific)</div>}
          <label className="toggle-row" style={{ marginTop: 14 }}>
            <span style={{ fontSize: 14, color: 'var(--text)' }}>Auto-restart when broadcast stops</span>
            <span className="toggle">
              <input type="checkbox" checked={yt?.auto_restart || false} onChange={toggleAutoRestart} disabled={!configured} />
              <span className="toggle-slider" />
            </span>
          </label>
        </div>

        {streamForm && <div className="card">
          <h2>Stream Output</h2>
          <div className="form-grid" style={{ marginBottom: 12 }}>
            <div className="field field-full"><label htmlFor="f-server">Server</label><input id="f-server" value={streamForm.rtmp_server} onChange={e => setStreamForm(f => ({ ...f, rtmp_server: e.target.value }))} placeholder="rtmp://a.rtmp.youtube.com/live2" autoComplete="off" spellCheck={false} /></div>
            <div className="field field-full">
              <label htmlFor="f-stream-key">Stream key</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <input id="f-stream-key" type={showKey ? 'text' : 'password'} value={streamForm.rtmp_key} style={{ flex: 1 }}
                  autoComplete="off" spellCheck={false} placeholder="xxxx-xxxx-xxxx-xxxx-xxxx"
                  onChange={e => {
                    const v = e.target.value;
                    // A full URL pasted here fills both fields
                    if (/^rtmps?:\/\//i.test(v.trim())) setStreamForm(f => ({ ...f, ...splitRtmp(v.trim()) }));
                    else setStreamForm(f => ({ ...f, rtmp_key: v }));
                  }} />
                <button className="btn btn-ghost" style={{ flexShrink: 0 }} onClick={() => setShowKey(v => !v)}>{showKey ? 'Hide' : 'Show'}</button>
              </div>
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>Anyone with this key can stream to your channel. Find both in YouTube Studio &rarr; Go live &rarr; Stream settings.</span>
            </div>
            <div className="field">
              <label htmlFor="f-resolution">Resolution</label>
              <select id="f-resolution" value={resValue} onChange={e => setResolution(e.target.value)}>
                {cfgResCustom && <option value={CURRENT}>Current: {config.output_width * 9 === config.output_height * 16 ? `${config.output_height}p${config.output_framerate}` : `${config.output_width}×${config.output_height} ${config.output_framerate}fps`}</option>}
                {YT_RESOLUTIONS.map(r => <option key={r.label} value={r.label}>{r.label}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="f-video-bitrate">Video Bitrate</label>
              <select id="f-video-bitrate" value={brValue} onChange={e => setBitrate(e.target.value)}>
                {cfgBrCustom && <option value={CURRENT}>Current: {config.video_bitrate_kbps / 1000} Mbps</option>}
                {YT_BITRATES.map(b => <option key={b.label} value={b.label}>{b.label}</option>)}
              </select>
            </div>
          </div>
          <button className="btn btn-ghost" onClick={saveStream}>Save</button>
        </div>}

        <div className="card">
          <h2>OAuth Credentials</h2>
          <div className="form-grid" style={{ marginBottom: 12 }}>
            <div className="field field-full"><label htmlFor="f-client-id">Client ID</label><input id="f-client-id" value={creds.youtube_client_id} onChange={e => setCreds(c => ({ ...c, youtube_client_id: e.target.value }))} placeholder="xxxxxx.apps.googleusercontent.com" /></div>
            <div className="field field-full"><label htmlFor="f-client-secret">Client Secret</label><input id="f-client-secret" type="password" value={creds.youtube_client_secret} onChange={e => setCreds(c => ({ ...c, youtube_client_secret: e.target.value }))} placeholder={secretSaved(config, 'youtube_client_secret') ? SECRET_PLACEHOLDER : ''} autoComplete="new-password" /></div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="btn btn-ghost" onClick={saveCreds} disabled={polling}>Save Credentials</button>
            {!configured
              ? <button className="btn btn-primary" onClick={startAuth} disabled={!creds.youtube_client_id || !(creds.youtube_client_secret || secretSaved(config, 'youtube_client_secret'))}>Authorise…</button>
              : <button className="btn btn-danger" onClick={revokeAuth}>Remove Token</button>
            }
          </div>
          {authFlow && (
            <div style={{ marginTop: 16, padding: '14px 16px', background: 'var(--surface2)', borderRadius: 8, border: '1px solid var(--border)' }}>
              <div style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 4 }}>Open this URL on any device and enter the code:</div>
              <div className="yt-auth-url">{authFlow.verification_url}</div>
              <div className="yt-auth-code">{authFlow.user_code}</div>
              <div style={{ fontSize: 12, color: authUnreachable ? 'var(--danger)' : 'var(--muted)', marginTop: 6 }}>{authUnreachable ? "Can’t reach the addon — still waiting" : 'Waiting for approval…'}</div>
            </div>
          )}
        </div>

        <div className="card">
          <h2>Setup Instructions</h2>
          <ol style={{ fontSize: 13, color: 'var(--muted)', lineHeight: 1.7, paddingLeft: 16 }}>
            <li>Go to Google Cloud Console and create a project</li>
            <li>Enable <strong style={{ color: 'var(--text)' }}>YouTube Data API v3</strong></li>
            <li>Create OAuth 2.0 credentials &mdash; type: <strong style={{ color: 'var(--text)' }}>TV and Limited Input</strong></li>
            <li>Enter Client ID and Client Secret above, then click Save</li>
            <li>Click <strong style={{ color: 'var(--text)' }}>Authorise</strong> and follow the device flow</li>
          </ol>
        </div>

      </div>
    </div>
  );
}

// ── Unsaved edits ─────────────────────────────────────────────────────────────
// Edits live in App (not in the tabs), so switching tabs never loses them. A draft holds only the
// fields its form edits, and exists only while it differs from the saved config.
function pick(obj, keys) {
  return Object.fromEntries(keys.map(k => [k, obj?.[k]]));
}
function draftFor(drafts, setDrafts, key, base) {
  const value = drafts[key] ?? base;
  const update = (fn) => setDrafts(d => {
    const next = fn(d[key] ?? base);
    const dirty = Object.keys(next).some(k => String(next[k] ?? '') !== String(base[k] ?? ''));
    const out = { ...d };
    if (dirty) out[key] = next; else delete out[key];
    return out;
  });
  const clear = () => setDrafts(d => { const out = { ...d }; delete out[key]; return out; });
  return [value, update, clear];
}
const SETTINGS_FIELDS = ['ha_url', 'ha_token', 'ha_entity_id', 'rtsp_latency_ms', 'reconnect_delay_sec',
                         'output_stall_timeout_sec', 'startup_output_timeout_sec', 'video_preview'];
// rtmp_url is edited as two fields (server + stream key) and joined back on save
const STREAM_FIELDS = ['output_width', 'output_height', 'output_framerate', 'video_bitrate_kbps'];
function splitRtmp(url) {
  url = url || '';
  const i = url.lastIndexOf('/');
  // Need a path after the host: "rtmp://host" alone has no key
  if (i < 0 || i <= url.indexOf('://') + 2) return { rtmp_server: url, rtmp_key: '' };
  return { rtmp_server: url.slice(0, i), rtmp_key: url.slice(i + 1) };
}
function joinRtmp(server, key) {
  server = (server || '').trim().replace(/\/+$/, '');
  key = (key || '').trim();
  return server || key ? `${server}/${key}` : '';
}

// ── Shared ────────────────────────────────────────────────────────────────────
// Settings the running video pipeline is built from (mirrors _PIPELINE_KEYS on the server):
// changing any of them restarts the video, which interrupts the YouTube stream for a few seconds
const PIPELINE_KEYS = ['rtmp_url', 'output_width', 'output_height', 'output_framerate', 'video_bitrate_kbps',
                       'rtsp_latency_ms', 'video_preview', 'output_stall_timeout_sec', 'startup_output_timeout_sec'];
// Ask before a save that would interrupt video going out to YouTube. Unknown sending state (no
// status yet) counts as sending, so the question is never skipped by mistake.
function confirmVideoRestart(oldCfg, newCfg, yt) {
  const restarts = PIPELINE_KEYS.some(k => String(oldCfg[k] ?? '') !== String(newCfg[k] ?? ''));
  if (!restarts || yt?.rtmp_sending === false) return true;
  return confirm('Saving restarts the video — YouTube viewers will see a few seconds of interruption. Continue?');
}

// Secrets the server never sends back: blank in config, listed in config._saved_secrets when set
const SECRET_KEYS = ['ha_token', 'youtube_client_secret'];
const secretSaved = (config, k) => (config?._saved_secrets || []).includes(k);
const SECRET_PLACEHOLDER = 'Saved — type to replace';

async function saveConfigDetailed(cfg) {
  try {
    const r = await apiFetch(`/api/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, error: d.error, field: d.field, restarted: !!d.restarted };
  } catch { return { ok: false, error: 'Could not reach the addon' }; }
}
async function saveConfig(cfg) {
  return (await saveConfigDetailed(cfg)).ok;
}

// ── App ───────────────────────────────────────────────────────────────────────
function App() {
  const [tab, setTab] = useState('cameras');
  const [config, setConfig] = useState(null);
  const [toast, setToast] = useState(null);
  const [yt, setYt] = useState(null);
  const [drafts, setDrafts] = useState({});
  const unsaved = { youtube: !!(drafts.ytCreds || drafts.ytStream), settings: !!drafts.settings };
  // After a save, drop any secret just typed so the page holds config the same way the server sends it
  const onConfigChange = (cfg) => setConfig(c => {
    const saved = new Set([...(c?._saved_secrets || []), ...SECRET_KEYS.filter(k => cfg[k])]);
    return { ...cfg, ...Object.fromEntries(SECRET_KEYS.map(k => [k, ''])), _saved_secrets: [...saved] };
  });

  // Retry until the config arrives, so a single failed request can't leave the tabs empty
  const loadConfig = useCallback(() => apiFetch(`/api/config`).then(r => r.ok ? r.json() : null)
    .then(c => { if (c) setConfig(c); return !!c; }).catch(() => false), []);
  usePoll('config', loadConfig, 3000, !config);

  // Local endpoint (no YouTube API cost) — poll often so the UI never trails the server's state
  const loadYt = useCallback(() => apiFetch(`/api/youtube/status`).then(r => r.json())
    .then(d => { setYt(d); return true; }).catch(() => false), []);
  usePoll('yt', loadYt, 3000);

  const toastTimer = React.useRef(null);
  const showToast = useCallback((msg, ok) => {
    setToast({ msg, ok });
    clearTimeout(toastTimer.current);  // an earlier toast's timer must not cut this one short
    toastTimer.current = setTimeout(() => setToast(null), 3000);
  }, []);

  // A restart only asks YouTube to go live; announce the outcome once YouTube settles it
  const prevYt = React.useRef(null);
  useEffect(() => {
    const prev = prevYt.current;
    prevYt.current = yt;
    if (!prev?.starting || !yt || yt.starting) return;
    if (yt.live) showToast('YouTube is live', true);
    else if (yt.last_error) showToast(yt.last_error, false);
  }, [yt, showToast]);

  const lost = useConnectionLost();
  const { resuming } = usePageState();
  const wasLost = React.useRef(false);
  useEffect(() => {
    // Back in contact: the addon may have restarted, so reload everything now rather than on the next tick
    if (wasLost.current && !lost) {
      apiFetch(`/api/config`).then(r => r.json()).then(setConfig).catch(() => {});
      apiFetch(`/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {});
    }
    wasLost.current = lost;
  }, [lost]);

  return (
    <>
      {lost && <ConnectionBanner />}
      <div className={`conn-stale${lost || resuming ? ' lost' : ''}`}>
      <div className="tabbar" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'cameras'} aria-label="Cameras" className={`tab${tab === 'cameras' ? ' active' : ''}`} onClick={() => setTab('cameras')} title="Cameras"><IconCamera /></button>
        <button type="button" role="tab" aria-selected={tab === 'youtube'} aria-label={'YouTube' + (unsaved.youtube ? ', unsaved changes' : '')} className={`tab${tab === 'youtube' ? ' active' : ''}`} onClick={() => setTab('youtube')} title="YouTube"><IconYouTube />{unsaved.youtube && <span className="tab-dot" title="Unsaved changes" />}</button>
        <button type="button" role="tab" aria-selected={tab === 'settings'} aria-label={'Settings' + (unsaved.settings ? ', unsaved changes' : '')} className={`tab${tab === 'settings' ? ' active' : ''}`} onClick={() => setTab('settings')} title="Settings"><IconSettings />{unsaved.settings && <span className="tab-dot" title="Unsaved changes" />}</button>
      </div>
      <YtStrip yt={yt} onSetup={!window.EMBED_MODE && tab !== 'youtube' ? () => setTab('youtube') : undefined} />
      {!config && <div className="content"><div className="loading-msg">{lost ? 'Waiting for the addon…' : 'Loading…'}</div></div>}
      {config && tab === 'cameras'  && <CameraTab config={config} onConfigChange={onConfigChange} showToast={showToast} />}
      {config && tab === 'youtube'  && <YouTubeTab config={config} onConfigChange={onConfigChange} showToast={showToast} yt={yt} setYt={setYt} drafts={drafts} setDrafts={setDrafts} />}
      {config && tab === 'settings' && <SettingsTab config={config} onConfigChange={onConfigChange} showToast={showToast} drafts={drafts} setDrafts={setDrafts} yt={yt} />}
      </div>
      <Toast toast={toast} />
    </>
  );
}

createRoot(document.getElementById('root')).render(<App />);
