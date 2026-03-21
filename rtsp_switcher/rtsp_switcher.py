#!/usr/bin/env python3
import multiprocessing
import os
import pathlib
import signal
import sys
import threading
import time

import json
import urllib.error
import urllib.parse
import urllib.request

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
# YouTube Live manager
# ---------------------------------------------------------------------------

_YT_TOKEN_URL = "https://oauth2.googleapis.com/token"
_YT_DEVICE_URL = "https://oauth2.googleapis.com/device/code"
_YT_API = "https://www.googleapis.com/youtube/v3"


def _yt_request(req):
    """Execute a urllib request and return parsed JSON, or raise."""
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _yt_start_device_flow(client_id: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": "https://www.googleapis.com/auth/youtube",
    }).encode()
    req = urllib.request.Request(_YT_DEVICE_URL, data=data, method="POST")
    return _yt_request(req)


def _yt_poll_device_token(client_id: str, client_secret: str, device_code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }).encode()
    req = urllib.request.Request(_YT_TOKEN_URL, data=data, method="POST")
    try:
        return _yt_request(req)
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
        raise RuntimeError(body.get("error", "unknown_error")) from exc


class YouTubeManager(threading.Thread):
    """Background thread: monitors broadcast status and optionally auto-restarts."""

    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._status: dict = {
            "live": False, "broadcast_id": None, "title": None, "stream_health": None,
            "last_error": None, "started_at": None, "ended_at": None,
        }
        self._auto_restart: bool = False
        self._stopping = threading.Event()
        self._poll_now = threading.Event()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _refresh_access_token(self) -> str | None:
        cfg = _get_cfg()
        client_id = cfg.get("youtube_client_id", "")
        client_secret = cfg.get("youtube_client_secret", "")
        refresh_token = cfg.get("youtube_refresh_token", "")
        if not (client_id and client_secret and refresh_token):
            return None
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(_YT_TOKEN_URL, data=data, method="POST")
        try:
            result = _yt_request(req)
            self._access_token = result["access_token"]
            self._token_expires_at = time.monotonic() + result.get("expires_in", 3600) - 60
            return self._access_token
        except Exception as exc:
            print(f"[youtube] Token refresh failed: {exc}", flush=True)
            return None

    def _get_token(self) -> str | None:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        return self._refresh_access_token()

    # ── API helpers ───────────────────────────────────────────────────────────

    def _api_get(self, path: str, params: dict | None = None) -> dict:
        token = self._get_token()
        if not token:
            raise RuntimeError("No access token — check OAuth credentials")
        url = f"{_YT_API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            return _yt_request(req)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                detail = json.loads(body).get("error", {}).get("message", body.decode())
            except Exception:
                detail = body.decode(errors="replace")
            raise RuntimeError(f"GET {path}: {exc.code} {detail}") from exc

    def _api_post(self, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        token = self._get_token()
        if not token:
            raise RuntimeError("No access token — check OAuth credentials")
        url = f"{_YT_API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            return _yt_request(req)
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            try:
                detail = json.loads(body_bytes).get("error", {}).get("message", body_bytes.decode())
            except Exception:
                detail = body_bytes.decode(errors="replace")
            raise RuntimeError(f"POST {path}: {exc.code} {detail}") from exc

    # ── Public accessors ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def set_auto_restart(self, enabled: bool):
        with self._lock:
            self._auto_restart = enabled

    # ── YouTube workflow ──────────────────────────────────────────────────────

    def _get_active_broadcast(self) -> dict | None:
        result = self._api_get("liveBroadcasts", {
            "part": "snippet,status,contentDetails", "broadcastStatus": "active",
        })
        items = (result or {}).get("items", [])
        return items[0] if items else None

    def _get_stream_health(self, stream_id: str) -> dict:
        """Returns healthStatus dict from the liveStream, or {} on failure."""
        result = self._api_get("liveStreams", {"part": "status", "id": stream_id})
        items = (result or {}).get("items", [])
        if not items:
            return {}
        return items[0].get("status", {}).get("healthStatus", {})

    def _get_live_stream(self) -> dict | None:
        """Find the liveStream whose streamName matches our RTMP stream key."""
        rtmp_url = _get_cfg().get("rtmp_url", "")
        stream_key = rtmp_url.rsplit("/", 1)[-1] if "/" in rtmp_url else ""
        if not stream_key:
            return None
        result = self._api_get("liveStreams", {"part": "cdn,status", "mine": "true", "maxResults": "50"})
        for item in (result or {}).get("items", []):
            if item.get("cdn", {}).get("ingestionInfo", {}).get("streamName") == stream_key:
                return item
        return None

    def _get_last_broadcast(self) -> dict | None:
        result = self._api_get("liveBroadcasts", {
            "part": "snippet,status,contentDetails", "broadcastStatus": "completed", "maxResults": "1",
        })
        items = (result or {}).get("items", [])
        return items[0] if items else None

    def _create_broadcast(self, source: dict | None) -> str | None:
        """Create a new broadcast, copying all writable fields from source if provided."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if source:
            src_snippet = source.get("snippet", {})
            src_status = source.get("status", {})
            src_cd = source.get("contentDetails", {})
            src_monitor = src_cd.get("monitorStream", {})

            snippet = {
                "title": src_snippet.get("title", "Live Stream"),
                "description": src_snippet.get("description", ""),
                "scheduledStartTime": now,
            }
            status = {
                "privacyStatus": src_status.get("privacyStatus", "public"),
                "selfDeclaredMadeForKids": src_status.get("selfDeclaredMadeForKids", False),
            }
            content_details = {}
            for key in ("enableDvr", "enableContentEncryption", "enableEmbed",
                        "recordFromStart", "startWithSlate", "projection",
                        "latencyPreference", "enableAutoStart", "enableAutoStop",
                        "closedCaptionsType"):
                if key in src_cd:
                    content_details[key] = src_cd[key]
            if src_monitor:
                monitor = {}
                for key in ("enableMonitorStream", "broadcastStreamDelayMs"):
                    if key in src_monitor:
                        monitor[key] = src_monitor[key]
                if monitor:
                    content_details["monitorStream"] = monitor
        else:
            snippet = {"title": "Live Stream", "description": "", "scheduledStartTime": now}
            status = {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
            content_details = {}

        body = {"snippet": snippet, "status": status, "contentDetails": content_details}
        result = self._api_post("liveBroadcasts", body, {"part": "snippet,status,contentDetails"})
        return (result or {}).get("id")

    def _bind_broadcast(self, broadcast_id: str, stream_id: str):
        self._api_post("liveBroadcasts/bind", params={
            "id": broadcast_id, "streamId": stream_id, "part": "snippet",
        })

    def _transition_to_live(self, broadcast_id: str):
        self._api_post("liveBroadcasts/transition", params={
            "broadcastStatus": "live", "id": broadcast_id, "part": "status",
        })

    def restart_broadcast(self) -> tuple[bool, str]:
        """Full restart workflow. Returns (success, message)."""
        try:
            if self._get_active_broadcast():
                return True, "Already live"

            live_stream = self._get_live_stream()
            if not live_stream:
                return False, "No liveStream found matching RTMP stream key"
            stream_id = live_stream["id"]

            last = self._get_last_broadcast()
            broadcast_id = self._create_broadcast(last)
            if not broadcast_id:
                return False, "Failed to create broadcast"

            self._bind_broadcast(broadcast_id, stream_id)

            # Wait up to 30s for the liveStream to become active
            for _ in range(30):
                stream_data = self._api_get("liveStreams", {"part": "status", "id": stream_id})
                items = (stream_data or {}).get("items", [])
                if items and items[0].get("status", {}).get("streamStatus") == "active":
                    break
                time.sleep(1)
            else:
                return False, "Stream not active after 30s — is RTMP pushing?"

            self._transition_to_live(broadcast_id)
            print(f"[youtube] Restarted broadcast {broadcast_id!r} ({title!r})", flush=True)
            return True, f"Broadcast restarted: {title}"

        except Exception as exc:
            return False, str(exc)

    # ── Background poll ───────────────────────────────────────────────────────

    def _poll(self):
        try:
            active = self._get_active_broadcast()
            if active:
                snippet = active.get("snippet", {})
                stream_id = active.get("contentDetails", {}).get("boundStreamId")
                health = self._get_stream_health(stream_id) if stream_id else {}
                with self._lock:
                    self._status = {
                        "live": True,
                        "broadcast_id": active.get("id"),
                        "title": snippet.get("title"),
                        "stream_health": health.get("status"),
                        "stream_issues": health.get("configurationIssues", []),
                        "last_error": None,
                        "started_at": snippet.get("actualStartTime"),
                        "ended_at": self._status.get("ended_at"),
                    }
            else:
                with self._lock:
                    prev_live = self._status.get("live", False)
                    ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if prev_live else self._status.get("ended_at")
                    self._status = {
                        "live": False, "broadcast_id": None, "title": None,
                        "stream_health": None, "stream_issues": [],
                        "last_error": None, "started_at": None, "ended_at": ended_at,
                    }
                    auto = self._auto_restart
                if auto:
                    print("[youtube] Broadcast not live, auto-restarting...", flush=True)
                    ok, msg = self.restart_broadcast()
                    print(f"[youtube] Auto-restart result: {msg}", flush=True)
        except Exception as exc:
            print(f"[youtube] Poll error: {exc}", flush=True)
            with self._lock:
                self._status["last_error"] = str(exc)

    def force_poll(self):
        self._poll_now.set()

    def run(self):
        while not self._stopping.is_set():
            cfg = _get_cfg()
            if cfg.get("youtube_refresh_token") and cfg.get("youtube_client_id") and cfg.get("youtube_client_secret"):
                self._poll()
            self._poll_now.clear()
            self._poll_now.wait(30)  # sleep 30s or wake early via force_poll() / stop()

    def stop(self):
        self._stopping.set()
        self._poll_now.set()  # break out of wait


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
html { height: 100%; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; background: var(--bg); color: var(--text); display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.tabbar { display: flex; border-bottom: 1px solid var(--border); background: var(--surface); flex-shrink: 0; }
.tab { padding: 12px 20px; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; margin-bottom: -1px; transition: color .12s; display: flex; align-items: center; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab svg { display: block; }
.yt-strip { display: flex; align-items: center; gap: 10px; padding: 7px 14px; background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0; min-height: 36px; }
.yt-strip-time { font-size: 11px; color: var(--muted); }
.yt-health-warn { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; color: #f5a623; }
.yt-health-err  { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; color: var(--danger); }
.issue-list { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
.issue-row { display: flex; gap: 8px; align-items: flex-start; padding: 8px 10px; border-radius: 6px; font-size: 12px; }
.issue-row.warning { background: rgba(245,166,35,.08); border: 1px solid rgba(245,166,35,.25); color: #f5a623; }
.issue-row.error   { background: rgba(224,85,85,.08);  border: 1px solid rgba(224,85,85,.25);  color: var(--danger); }
.issue-reason { color: var(--text); font-size: 12px; }
.content { flex: 1; min-height: 0; overflow-y: auto; }
.snapshot-outer { width: 100%; aspect-ratio: 16/9; background: #000; position: relative; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.snapshot-outer img { width: 100%; height: 100%; object-fit: contain; display: block; }
.snapshot-placeholder { color: var(--muted); font-size: 13px; position: absolute; }
.camera-section { padding: 12px 16px 20px; }
.camera-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.camera-row { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface); cursor: pointer; transition: background .12s, border-color .12s; user-select: none; }
.camera-row:hover { background: var(--surface2); }
.camera-row.active { background: rgba(62,207,142,.07); border-color: rgba(62,207,142,.25); }
.camera-name { font-size: 13px; font-weight: 500; color: var(--text); flex: 1; }
.live-dot { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; color: var(--success); white-space: nowrap; }
.live-dot::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--success); display: block; }
.cam-actions { display: flex; gap: 6px; flex-shrink: 0; }
.settings-content { padding: 20px 16px; }
h2 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); margin-bottom: 12px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 14px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field-full { grid-column: 1 / -1; }
label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
input, select { background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; color: var(--text); font-size: 13px; outline: none; width: 100%; transition: border-color .12s; }
input:focus, select:focus { border-color: var(--accent); }
input[type="password"] { font-family: monospace; }
.btn { display: inline-flex; align-items: center; gap: 5px; padding: 7px 13px; border-radius: 6px; font-size: 12px; font-weight: 500; cursor: pointer; border: none; outline: none; transition: filter .12s; white-space: nowrap; }
.btn:hover { filter: brightness(1.12); }
.btn:active { filter: brightness(.9); }
.btn-primary { background: var(--accent); color: #fff; }
.btn-danger { background: transparent; color: var(--danger); border: 1px solid rgba(224,85,85,.35); }
.btn-danger:hover { background: rgba(224,85,85,.1); filter: none; }
.btn-ghost { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }
.btn-add { width: 100%; padding: 10px; background: transparent; border: 1px dashed var(--border); color: var(--muted); border-radius: 8px; font-size: 13px; cursor: pointer; transition: border-color .12s, color .12s; }
.btn-add:hover { border-color: var(--accent); color: var(--accent); }
.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.65); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 22px; width: 480px; max-width: 95vw; }
.modal-title { font-size: 14px; font-weight: 600; color: #fff; margin-bottom: 18px; }
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.toast { position: fixed; bottom: 20px; right: 20px; padding: 10px 16px; border-radius: 8px; font-size: 13px; font-weight: 500; z-index: 200; border: 1px solid; }
.toast-ok { background: rgba(62,207,142,.1); border-color: rgba(62,207,142,.3); color: var(--success); }
.toast-err { background: rgba(224,85,85,.1); border-color: rgba(224,85,85,.3); color: var(--danger); }
.yt-status-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 20px; }
.yt-status-badge.live { background: rgba(62,207,142,.12); color: var(--success); border: 1px solid rgba(62,207,142,.3); }
.yt-status-badge.idle { background: rgba(107,111,138,.12); color: var(--muted); border: 1px solid var(--border); }
.yt-status-badge.live::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--success); display: block; }
.yt-auth-code { font-family: monospace; font-size: 22px; font-weight: 700; letter-spacing: .12em; color: #fff; padding: 10px 0 4px; }
.yt-auth-url { font-size: 12px; color: var(--muted); word-break: break-all; }
.toggle-row { display: flex; align-items: center; justify-content: space-between; }
.toggle { position: relative; width: 38px; height: 22px; flex-shrink: 0; }
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle-slider { position: absolute; inset: 0; background: var(--border); border-radius: 22px; transition: background .15s; cursor: pointer; }
.toggle-slider::before { content: ''; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: transform .15s; }
.toggle input:checked + .toggle-slider { background: var(--accent); }
.toggle input:checked + .toggle-slider::before { transform: translateX(16px); }
</style>
</head>
<body>
<div id="root"></div>
<script type="text/babel">
const { useState, useEffect, useCallback } = React;
const BASE = window.INGRESS_PATH || '';

// ── Icons ─────────────────────────────────────────────────────────────────────
const IconCamera = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>
    <circle cx="12" cy="13" r="3"/>
  </svg>
);
const IconSettings = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="3"/>
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
  </svg>
);
const IconYouTube = () => (
  <svg width="22" height="20" viewBox="0 0 24 24">
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
  const diff = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  const h = Math.floor(diff / 3600), m = Math.floor((diff % 3600) / 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
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

function YtStrip({ yt }) {
  const [, tick] = useState(0);
  useEffect(() => { const id = setInterval(() => tick(n => n + 1), 30000); return () => clearInterval(id); }, []);
  if (!yt?.configured) return <div className="yt-strip" />;
  const live = yt.live;
  return (
    <div className="yt-strip">
      <span className={`yt-status-badge ${live ? 'live' : 'idle'}`}>{live ? 'Live' : 'Idle'}</span>
      {live && yt.started_at && (
        <span className="yt-strip-time">since {fmtTime(yt.started_at)} &middot; {fmtElapsed(yt.started_at)}</span>
      )}
      {!live && yt.ended_at && (
        <span className="yt-strip-time">ended {fmtTime(yt.ended_at)}</span>
      )}
      {live && <HealthWarn health={yt.stream_health} issues={yt.stream_issues} />}
      {live && yt.title && (
        <span className="yt-strip-time" style={{ marginLeft: 'auto', maxWidth: '50%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{yt.title}</span>
      )}
    </div>
  );
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function Toast({ toast }) {
  if (!toast) return null;
  return <div className={`toast ${toast.ok ? 'toast-ok' : 'toast-err'}`}>{toast.msg}</div>;
}

// ── Snapshot ──────────────────────────────────────────────────────────────────
function Snapshot({ ts }) {
  const [ok, setOk] = useState(false);
  return (
    <div className="snapshot-outer">
      <img
        src={`${BASE}/api/snapshot?t=${ts}`}
        style={{ display: ok ? 'block' : 'none' }}
        onLoad={() => setOk(true)}
        onError={() => setOk(false)}
        alt=""
      />
      {!ok && <span className="snapshot-placeholder">No snapshot available</span>}
    </div>
  );
}

// ── Camera modal ──────────────────────────────────────────────────────────────
const BLANK_CAM = { stream_name: '', stream_url: '', stream_width: 1920, stream_height: 1080, stream_framerate: 30, stream_codec: 'h264', stream_rotation: 0 };

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
          <div className="field"><label>Width</label><input type="number" value={form.stream_width} onChange={e => num('stream_width', e.target.value)} /></div>
          <div className="field"><label>Height</label><input type="number" value={form.stream_height} onChange={e => num('stream_height', e.target.value)} /></div>
          <div className="field"><label>Framerate</label><input type="number" value={form.stream_framerate} onChange={e => num('stream_framerate', e.target.value)} /></div>
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
              <option value={0}>0\u00b0</option><option value={90}>90\u00b0</option>
              <option value={180}>180\u00b0</option><option value={270}>270\u00b0</option>
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

// ── Camera tab ────────────────────────────────────────────────────────────────
function CameraTab({ config, onConfigChange, showToast }) {
  const [status, setStatus] = useState(null);
  const [ts, setTs] = useState(Date.now());
  const [modal, setModal] = useState(null);

  useEffect(() => {
    const tick = () => {
      fetch(`${BASE}/api/status`).then(r => r.json()).then(setStatus).catch(() => {});
      setTs(Date.now());
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => clearInterval(id);
  }, []);

  if (!config) return null;
  const streams = config.streams || [];

  const switchTo = async (name) => {
    setStatus(s => ({ ...s, active_stream: name, streaming: true }));
    await fetch(`${BASE}/api/switch`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
  };

  const persist = async (newStreams) => {
    const newCfg = { ...config, streams: newStreams };
    const ok = await saveConfig(newCfg);
    if (ok) { onConfigChange(newCfg); showToast('Saved', true); }
    else showToast('Save failed', false);
  };

  const handleSave = (form) => {
    const newStreams = modal === 'add' ? [...streams, form] : streams.map((s, i) => i === modal ? form : s);
    persist(newStreams);
    setModal(null);
  };

  const handleDelete = (e, idx) => {
    e.stopPropagation();
    if (!confirm(`Delete "${streams[idx].stream_name}"?`)) return;
    persist(streams.filter((_, i) => i !== idx));
  };

  const active = status?.active_stream;

  return (
    <div className="content">
      <Snapshot ts={ts} />
      <div className="camera-section">
        <div className="camera-list">
          {streams.map((s, i) => {
            const isActive = s.stream_name === active;
            return (
              <div key={i} className={`camera-row${isActive ? ' active' : ''}`} onClick={() => switchTo(s.stream_name)}>
                <span className="camera-name">{s.stream_name}</span>
                {isActive && <span className="live-dot">Live</span>}
                <div className="cam-actions">
                  <button className="btn btn-ghost" onClick={e => { e.stopPropagation(); setModal(i); }}>Edit</button>
                  <button className="btn btn-danger" onClick={e => handleDelete(e, i)}>Delete</button>
                </div>
              </div>
            );
          })}
        </div>
        <button className="btn-add" onClick={() => setModal('add')}>+ Add Camera</button>
      </div>
      {modal !== null && (
        <CameraModal initial={modal === 'add' ? null : streams[modal]} onSave={handleSave} onClose={() => setModal(null)} />
      )}
    </div>
  );
}

// ── Settings tab ──────────────────────────────────────────────────────────────
function SettingsTab({ config, onConfigChange, showToast }) {
  const [form, setForm] = useState(null);
  useEffect(() => { if (config) setForm({ ...config }); }, [config]);
  if (!form) return null;

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const num = (k, v) => set(k, parseInt(v) || 0);

  const handleSave = async () => {
    const ok = await saveConfig(form);
    if (ok) { onConfigChange(form); showToast('Saved', true); }
    else showToast('Save failed', false);
  };

  return (
    <div className="content">
      <div className="settings-content">
        <div className="card">
          <h2>Stream Output</h2>
          <div className="form-grid">
            <div className="field field-full"><label>RTMP URL</label><input value={form.rtmp_url || ''} onChange={e => set('rtmp_url', e.target.value)} placeholder="rtmp://a.rtmp.youtube.com/live2/..." /></div>
            <div className="field"><label>Output Width</label><input type="number" value={form.output_width || ''} onChange={e => num('output_width', e.target.value)} /></div>
            <div className="field"><label>Output Height</label><input type="number" value={form.output_height || ''} onChange={e => num('output_height', e.target.value)} /></div>
            <div className="field"><label>Video Bitrate (kbps)</label><input type="number" value={form.video_bitrate_kbps || ''} onChange={e => num('video_bitrate_kbps', e.target.value)} /></div>
            <div className="field"><label>Output Framerate</label><input type="number" value={form.output_framerate || ''} onChange={e => num('output_framerate', e.target.value)} /></div>
          </div>
        </div>
        <div className="card">
          <h2>Home Assistant</h2>
          <div className="form-grid">
            <div className="field field-full"><label>WebSocket URL</label><input value={form.ha_url || ''} onChange={e => set('ha_url', e.target.value)} placeholder="ws://homeassistant:8123/api/websocket" /></div>
            <div className="field field-full"><label>Long-Lived Access Token</label><input type="password" value={form.ha_token || ''} onChange={e => set('ha_token', e.target.value)} /></div>
            <div className="field field-full"><label>Entity ID</label><input value={form.ha_entity_id || ''} onChange={e => set('ha_entity_id', e.target.value)} placeholder="input_select.camera_view" /></div>
          </div>
        </div>
        <div className="card">
          <h2>Advanced</h2>
          <div className="form-grid">
            <div className="field"><label>RTSP Latency (ms)</label><input type="number" value={form.rtsp_latency_ms || ''} onChange={e => num('rtsp_latency_ms', e.target.value)} /></div>
            <div className="field"><label>Reconnect Delay (s)</label><input type="number" value={form.reconnect_delay_sec || ''} onChange={e => num('reconnect_delay_sec', e.target.value)} /></div>
            <div className="field"><label>Output Stall Timeout (s)</label><input type="number" value={form.output_stall_timeout_sec || ''} onChange={e => num('output_stall_timeout_sec', e.target.value)} /></div>
            <div className="field"><label>Startup Output Timeout (s)</label><input type="number" value={form.startup_output_timeout_sec || ''} onChange={e => num('startup_output_timeout_sec', e.target.value)} /></div>
          </div>
        </div>
        <button className="btn btn-primary" onClick={handleSave}>Save Settings</button>
      </div>
    </div>
  );
}

// ── YouTube tab ───────────────────────────────────────────────────────────────
function YouTubeTab({ config, onConfigChange, showToast, yt, setYt }) {
  const [authFlow, setAuthFlow] = useState(null); // {user_code, verification_url}
  const [polling, setPolling] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [creds, setCreds] = useState({ youtube_client_id: '', youtube_client_secret: '' });

  useEffect(() => {
    if (config) setCreds({ youtube_client_id: config.youtube_client_id || '', youtube_client_secret: config.youtube_client_secret || '' });
  }, [config]);

  // While device flow is active, poll every 5s
  useEffect(() => {
    if (!authFlow) return;
    const id = setInterval(async () => {
      const r = await fetch(`${BASE}/api/youtube/auth/poll`, { method: 'POST' });
      const d = await r.json();
      if (d.ok) {
        setAuthFlow(null);
        showToast('YouTube authorised', true);
        fetch(`${BASE}/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {});
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
    if (ok) { onConfigChange(newCfg); showToast('Saved', true); }
    else showToast('Save failed', false);
  };

  const startAuth = async () => {
    try {
      const r = await fetch(`${BASE}/api/youtube/auth/start`, { method: 'POST' });
      const d = await r.json();
      if (d.error) { showToast(d.error, false); return; }
      setAuthFlow(d);
    } catch { showToast('Failed to start auth', false); }
  };

  const revokeAuth = async () => {
    if (!confirm('Remove stored refresh token?')) return;
    const newCfg = { ...config, youtube_refresh_token: '' };
    if (await saveConfig(newCfg)) { onConfigChange(newCfg); showToast('Token removed', true); }
  };

  const doRefresh = async () => {
    await fetch(`${BASE}/api/youtube/poll`, { method: 'POST' });
    // give the background thread a moment to complete the poll
    setTimeout(() => fetch(`${BASE}/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {}), 2000);
  };

  const doRestart = async () => {
    setRestarting(true);
    try {
      const r = await fetch(`${BASE}/api/youtube/restart`, { method: 'POST' });
      const d = await r.json();
      showToast(d.message || (d.ok ? 'Done' : 'Failed'), d.ok);
    } catch { showToast('Request failed', false); }
    setRestarting(false);
    fetch(`${BASE}/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {});
  };

  const toggleAutoRestart = async (e) => {
    const enabled = e.target.checked;
    const r = await fetch(`${BASE}/api/youtube/auto_restart`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) });
    if (r.ok) setYt(s => ({ ...s, auto_restart: enabled }));
  };

  const live = yt?.live;
  const configured = yt?.configured;

  return (
    <div className="content">
      <div className="settings-content">

        <div className="card">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <h2 style={{ marginBottom: 0 }}>Broadcast Status</h2>
            <span className={`yt-status-badge ${live ? 'live' : 'idle'}`}>{live ? 'Live' : 'Idle'}</span>
          </div>
          {yt?.title && <div style={{ fontSize: 13, color: 'var(--text)', marginBottom: 4 }}>{yt.title}</div>}
          {live && yt?.started_at && <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>Started {fmtTime(yt.started_at)} &middot; {fmtElapsed(yt.started_at)}</div>}
          {!live && yt?.ended_at && <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>Ended {fmtTime(yt.ended_at)}</div>}
          {!configured && <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>Not authorised — complete OAuth setup below.</div>}
          {live && yt?.stream_issues?.length > 0 && (
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
            <button className="btn btn-primary" onClick={doRestart} disabled={restarting || !configured}>
              {restarting ? 'Restarting\u2026' : 'Restart Broadcast'}
            </button>
            <button className="btn btn-ghost" onClick={doRefresh}>Refresh</button>
          </div>
          {yt?.last_error && <div style={{ fontSize: 12, color: 'var(--danger)', marginTop: 8, wordBreak: 'break-word' }}>{yt.last_error}</div>}
          <div className="toggle-row" style={{ marginTop: 14 }}>
            <span style={{ fontSize: 13, color: 'var(--text)' }}>Auto-restart when broadcast stops</span>
            <label className="toggle">
              <input type="checkbox" checked={yt?.auto_restart || false} onChange={toggleAutoRestart} disabled={!configured} />
              <span className="toggle-slider" />
            </label>
          </div>
        </div>

        <div className="card">
          <h2>OAuth Credentials</h2>
          <div className="form-grid" style={{ marginBottom: 12 }}>
            <div className="field field-full"><label>Client ID</label><input value={creds.youtube_client_id} onChange={e => setCreds(c => ({ ...c, youtube_client_id: e.target.value }))} placeholder="xxxxxx.apps.googleusercontent.com" /></div>
            <div className="field field-full"><label>Client Secret</label><input type="password" value={creds.youtube_client_secret} onChange={e => setCreds(c => ({ ...c, youtube_client_secret: e.target.value }))} /></div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="btn btn-ghost" onClick={saveCreds} disabled={polling}>Save Credentials</button>
            {!configured
              ? <button className="btn btn-primary" onClick={startAuth} disabled={!creds.youtube_client_id || !creds.youtube_client_secret}>Authorise\u2026</button>
              : <button className="btn btn-danger" onClick={revokeAuth}>Remove Token</button>
            }
          </div>
          {authFlow && (
            <div style={{ marginTop: 16, padding: '14px 16px', background: 'var(--surface2)', borderRadius: 8, border: '1px solid var(--border)' }}>
              <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>Open this URL on any device and enter the code:</div>
              <div className="yt-auth-url">{authFlow.verification_url}</div>
              <div className="yt-auth-code">{authFlow.user_code}</div>
              <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>Waiting for approval\u2026</div>
            </div>
          )}
        </div>

        <div className="card">
          <h2>Setup Instructions</h2>
          <ol style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.7, paddingLeft: 16 }}>
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

// ── Shared ────────────────────────────────────────────────────────────────────
async function saveConfig(cfg) {
  try {
    const r = await fetch(`${BASE}/api/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
    return r.ok;
  } catch { return false; }
}

// ── App ───────────────────────────────────────────────────────────────────────
function App() {
  const [tab, setTab] = useState('cameras');
  const [config, setConfig] = useState(null);
  const [toast, setToast] = useState(null);
  const [yt, setYt] = useState(null);

  useEffect(() => {
    fetch(`${BASE}/api/config`).then(r => r.json()).then(setConfig).catch(() => {});
  }, []);

  useEffect(() => {
    const tick = () => fetch(`${BASE}/api/youtube/status`).then(r => r.json()).then(setYt).catch(() => {});
    tick();
    const id = setInterval(tick, 15000);
    return () => clearInterval(id);
  }, []);

  const showToast = useCallback((msg, ok) => {
    setToast({ msg, ok });
    setTimeout(() => setToast(null), 3000);
  }, []);

  return (
    <>
      <div className="tabbar">
        <div className={`tab${tab === 'cameras' ? ' active' : ''}`} onClick={() => setTab('cameras')} title="Cameras"><IconCamera /></div>
        <div className={`tab${tab === 'youtube' ? ' active' : ''}`} onClick={() => setTab('youtube')} title="YouTube"><IconYouTube /></div>
        <div className={`tab${tab === 'settings' ? ' active' : ''}`} onClick={() => setTab('settings')} title="Settings"><IconSettings /></div>
      </div>
      <YtStrip yt={yt} />
      {tab === 'cameras'  && <CameraTab config={config} onConfigChange={setConfig} showToast={showToast} />}
      {tab === 'youtube'  && <YouTubeTab config={config} onConfigChange={setConfig} showToast={showToast} yt={yt} setYt={setYt} />}
      {tab === 'settings' && <SettingsTab config={config} onConfigChange={setConfig} showToast={showToast} />}
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
_ha_listener_ref = None
_youtube_manager_ref: YouTubeManager | None = None
_yt_device_flow_state: dict = {}  # device_code while auth is in progress


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


@_flask_app.route("/api/switch", methods=["POST"])
def _api_switch():
    data = request.get_json(force=True)
    name = (data or {}).get("name")
    if not name:
        return jsonify({"error": "name required"}), 400

    if _ha_listener_ref:
        try:
            _ha_listener_ref.select_option(name)
            return jsonify({"ok": True})
        except Exception:
            pass  # fall through to direct switch

    if _manager_ref:
        _manager_ref.switch_stream(name)
    return jsonify({"ok": True})


@_flask_app.route("/api/status")
def _api_status():
    m = _manager_ref
    stream = m.current_stream if m else None
    return jsonify({"active_stream": stream, "streaming": stream is not None})


@_flask_app.route("/api/youtube/status")
def _api_youtube_status():
    m = _youtube_manager_ref
    cfg = _get_cfg()
    configured = bool(cfg.get("youtube_refresh_token") and cfg.get("youtube_client_id") and cfg.get("youtube_client_secret"))
    status = m.get_status() if m else {"live": False, "broadcast_id": None, "title": None, "stream_health": None}
    status["configured"] = configured
    status["auto_restart"] = cfg.get("youtube_auto_restart", False)
    return jsonify(status)


@_flask_app.route("/api/youtube/auth/start", methods=["POST"])
def _api_youtube_auth_start():
    global _yt_device_flow_state
    cfg = _get_cfg()
    client_id = cfg.get("youtube_client_id", "")
    if not client_id:
        return jsonify({"error": "youtube_client_id not configured"}), 400
    try:
        result = _yt_start_device_flow(client_id)
        _yt_device_flow_state = {"device_code": result["device_code"]}
        return jsonify({
            "user_code": result["user_code"],
            "verification_url": result["verification_url"],
            "expires_in": result.get("expires_in", 1800),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@_flask_app.route("/api/youtube/auth/poll", methods=["POST"])
def _api_youtube_auth_poll():
    global _yt_device_flow_state
    device_code = _yt_device_flow_state.get("device_code")
    if not device_code:
        return jsonify({"error": "No active auth flow"}), 400
    cfg = _get_cfg()
    try:
        result = _yt_poll_device_token(
            cfg.get("youtube_client_id", ""),
            cfg.get("youtube_client_secret", ""),
            device_code,
        )
        new_cfg = dict(cfg)
        new_cfg["youtube_refresh_token"] = result["refresh_token"]
        _save_config(new_cfg)
        _yt_device_flow_state = {}
        return jsonify({"ok": True})
    except RuntimeError as exc:
        err = str(exc)
        if err == "authorization_pending":
            return jsonify({"pending": True})
        return jsonify({"error": err}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@_flask_app.route("/api/youtube/restart", methods=["POST"])
def _api_youtube_restart():
    m = _youtube_manager_ref
    if not m:
        return jsonify({"error": "YouTube manager not running"}), 503
    ok, msg = m.restart_broadcast()
    return jsonify({"ok": ok, "message": msg})


@_flask_app.route("/api/youtube/poll", methods=["POST"])
def _api_youtube_poll():
    m = _youtube_manager_ref
    if not m:
        return jsonify({"error": "YouTube manager not running"}), 503
    m.force_poll()
    return jsonify({"ok": True})


@_flask_app.route("/api/youtube/auto_restart", methods=["POST"])
def _api_youtube_auto_restart():
    data = request.get_json(force=True)
    enabled = bool((data or {}).get("enabled", False))
    cfg = _get_cfg()
    new_cfg = dict(cfg)
    new_cfg["youtube_auto_restart"] = enabled
    _save_config(new_cfg)
    if _youtube_manager_ref:
        _youtube_manager_ref.set_auto_restart(enabled)
    return jsonify({"ok": True})


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

    def select_option(self, name: str):
        cfg = _get_cfg()
        try:
            self._client.trigger_service(
                "input_select", "select_option",
                entity_id=cfg["ha_entity_id"],
                option=name,
            )
        except Exception as exc:
            print(f"[ha] select_option failed: {exc}", flush=True)
            raise

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

    global _youtube_manager_ref
    yt_manager = YouTubeManager()
    _youtube_manager_ref = yt_manager
    yt_manager.set_auto_restart(_get_cfg().get("youtube_auto_restart", False))
    yt_manager.start()

    cfg = _get_cfg()
    ha_listener = None
    if cfg.get("ha_url") and cfg.get("ha_token"):
        ha_listener = HomeAssistantListener(manager.switch_stream)
        ha_listener.start()
        global _ha_listener_ref
        _ha_listener_ref = ha_listener

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
    yt_manager.stop()
    if ha_listener:
        ha_listener.stop()


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
