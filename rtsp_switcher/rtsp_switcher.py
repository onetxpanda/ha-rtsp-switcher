#!/usr/bin/env python3
import datetime
import multiprocessing
import os
import pathlib
import signal
import sys
import threading
import time
import zoneinfo

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import queue as _queue_mod

import yaml
from flask import Flask, Response, abort, jsonify, request, send_file
from flask_sock import Sock as _Sock
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
_YT_POLL_SEC = 60            # broadcast status poll (1 unit)
_YT_HEALTH_POLL_SEC = 60     # stream health poll while live (1 unit)
_YT_FAST_POLL_SEC = 10       # poll interval while confirming a change we triggered
_YT_FAST_POLL_WINDOW_SEC = 120
_YT_DAILY_QUOTA_DEFAULT = 10000
_YT_SLOW_POLL_QUOTA_FRACTION = 0.8      # above this share of the budget, poll slowly to keep units for restarts
_YT_SLOW_POLL_SEC = 300
_YT_MIN_SENDING_SEC = 15      # pipeline must be outputting this long before auto-restart tries


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pacific_tz():
    # YouTube API quota resets at midnight Pacific time
    try:
        return zoneinfo.ZoneInfo("America/Los_Angeles")
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=-8))


def _pacific_now() -> datetime.datetime:
    return datetime.datetime.now(_pacific_tz())


def _next_pacific_midnight() -> float:
    now = _pacific_now()
    tomorrow = (now + datetime.timedelta(days=1)).date()
    midnight = datetime.datetime.combine(tomorrow, datetime.time(0, 5), tzinfo=now.tzinfo)
    return midnight.timestamp()


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
        self._restart_lock = threading.Lock()
        self._auto_restart_backoff: float = 0.0     # seconds; 0 = no failures yet
        self._auto_restart_retry_at: float = 0.0    # monotonic time of next allowed auto-restart
        self._quota_day = None                      # Pacific-time date the counter applies to
        self._quota_used: int = 0                   # estimated units spent today (resets on addon restart)
        self._quota_paused_until: float = 0.0       # epoch time; set when the API reports quotaExceeded
        self._last_health_at: float = 0.0           # monotonic time of last stream health fetch
        self._last_health: dict = {}
        self._fast_poll_until: float = 0.0          # monotonic; poll fast until then or until confirmed
        self._expect_live: bool | None = None       # state we're waiting to see confirmed
        self.poll_interval_sec: int = _YT_POLL_SEC  # current cadence, reported to the UI
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
            print("[youtube] If tokens expire within days, ensure the Google Cloud OAuth consent screen is set to 'Production' (not 'Testing')", flush=True)
            self._access_token = None
            self._token_expires_at = 0.0
            return None

    def _get_token(self) -> str | None:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        return self._refresh_access_token()

    # ── Quota ─────────────────────────────────────────────────────────────────

    def _roll_quota_day(self):
        """Caller holds self._lock. Reset the usage counter when the Pacific-time day changes."""
        day = _pacific_now().date()
        if day != self._quota_day:
            self._quota_day = day
            self._quota_used = 0

    def quota_paused_until(self) -> float | None:
        """Epoch time API calls are paused until (quota exhausted), or None."""
        with self._lock:
            if self._quota_paused_until and time.time() < self._quota_paused_until:
                return self._quota_paused_until
            return None

    def quota_used(self) -> int:
        with self._lock:
            self._roll_quota_day()
            return self._quota_used

    def _spend_quota(self, units: int):
        with self._lock:
            if self._quota_paused_until and time.time() < self._quota_paused_until:
                resume = time.strftime("%H:%M", time.localtime(self._quota_paused_until))
                raise RuntimeError(f"YouTube API quota exhausted — paused until {resume}")
            self._roll_quota_day()
            self._quota_used += units

    def _pause_for_quota(self):
        with self._lock:
            self._quota_paused_until = _next_pacific_midnight()
            resume = time.strftime("%Y-%m-%d %H:%M", time.localtime(self._quota_paused_until))
        print(f"[youtube] API quota exceeded — pausing all YouTube API calls until {resume}", flush=True)

    # ── API helpers ───────────────────────────────────────────────────────────

    def _api(self, method: str, path: str, params: dict | None = None,
             body: dict | None = None, cost: int = 1) -> dict:
        token = self._get_token()
        if not token:
            raise RuntimeError("No access token — check OAuth credentials")
        self._spend_quota(cost)
        url = f"{_YT_API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                err = json.loads(raw).get("error", {})
                detail = err.get("message", raw.decode())
                reasons = {e.get("reason") for e in err.get("errors", [])}
            except Exception:
                detail = raw.decode(errors="replace")
                reasons = set()
            if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons:
                self._pause_for_quota()
            raise RuntimeError(f"{method} {path}: {exc.code} {detail}") from exc

    def _api_get(self, path: str, params: dict | None = None) -> dict:
        return self._api("GET", path, params=params, cost=1)

    def _api_post(self, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        # insert, bind and transition each cost 50 units
        return self._api("POST", path, params=params, body=body, cost=50)

    def _api_delete(self, path: str, params: dict | None = None):
        self._api("DELETE", path, params=params, cost=50)

    # ── Public accessors ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def set_auto_restart(self, enabled: bool):
        with self._lock:
            self._auto_restart = enabled
            self._auto_restart_backoff = 0.0
            self._auto_restart_retry_at = 0.0

    def reset_auth(self):
        """Forget cached credentials and live status (refresh token was revoked)."""
        with self._lock:
            self._access_token = None
            self._token_expires_at = 0.0
            prev_live = self._status.get("live", False)
            self._expect_live = None
            self._fast_poll_until = 0.0
            self._status = {
                "live": False, "starting": False, "broadcast_id": None, "title": None,
                "stream_health": None, "stream_issues": [],
                "last_error": None, "started_at": None, "checked_at": None,
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if prev_live else self._status.get("ended_at"),
            }

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

    def _get_stale_broadcasts(self, stream_id: str) -> list[dict]:
        """Not-yet-live broadcasts bound to stream_id whose scheduled start has passed (left behind
        by failed restarts), newest first. Future-scheduled broadcasts are excluded."""
        result = self._api_get("liveBroadcasts", {
            "part": "snippet,status,contentDetails", "broadcastStatus": "upcoming", "maxResults": "50",
        })
        now = datetime.datetime.now(datetime.timezone.utc)
        stale = []
        for item in (result or {}).get("items", []):
            if item.get("contentDetails", {}).get("boundStreamId") != stream_id:
                continue
            try:
                scheduled = datetime.datetime.fromisoformat(item["snippet"]["scheduledStartTime"])
            except (KeyError, ValueError):
                continue
            if scheduled <= now:
                stale.append((scheduled, item))
        stale.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in stale]

    def _delete_broadcast(self, broadcast_id: str):
        try:
            self._api_delete("liveBroadcasts", {"id": broadcast_id})
            print(f"[youtube] Deleted stale broadcast {broadcast_id!r}", flush=True)
        except Exception as exc:
            print(f"[youtube] Could not delete broadcast {broadcast_id!r}: {exc}", flush=True)

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
                        "latencyPreference", "closedCaptionsType"):
                if key in src_cd:
                    content_details[key] = src_cd[key]
            if src_monitor:
                monitor = {}
                for key in ("enableMonitorStream", "broadcastStreamDelayMs"):
                    if key in src_monitor:
                        monitor[key] = src_monitor[key]
                if monitor:
                    content_details["monitorStream"] = monitor
            content_details["enableAutoStart"] = False
            content_details["enableAutoStop"] = False
        else:
            snippet = {"title": "Live Stream", "description": "", "scheduledStartTime": now}
            status = {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
            content_details = {"enableAutoStart": False, "enableAutoStop": False}

        body = {"snippet": snippet, "status": status, "contentDetails": content_details}
        result = self._api_post("liveBroadcasts", body, {"part": "snippet,status,contentDetails"})
        return (result or {}).get("id")

    def _bind_broadcast(self, broadcast_id: str, stream_id: str):
        self._api_post("liveBroadcasts/bind", params={
            "id": broadcast_id, "streamId": stream_id, "part": "snippet",
        })

    def _transition(self, broadcast_id: str, to: str):
        self._api_post("liveBroadcasts/transition", params={
            "broadcastStatus": to, "id": broadcast_id, "part": "status",
        })

    def _wait_for_lifecycle(self, broadcast_id: str, wanted: tuple, timeout_sec: int,
                            interval_sec: int) -> tuple[str | None, dict]:
        """Poll the broadcast until its lifeCycleStatus is in wanted or timeout. Returns (status, item)."""
        deadline = time.monotonic() + timeout_sec
        while True:
            data = self._api_get("liveBroadcasts", {"part": "status,contentDetails", "id": broadcast_id})
            items = (data or {}).get("items", [])
            item = items[0] if items else {}
            status = item.get("status", {}).get("lifeCycleStatus")
            if status in wanted or time.monotonic() >= deadline:
                return status, item
            time.sleep(interval_sec)

    def restart_broadcast(self) -> tuple[bool, str]:
        """Full restart workflow. Returns (success, message)."""
        if _manager_ref is not None and not _manager_ref.output_active:
            return False, "Not sending video to YouTube — the camera pipeline isn't producing output"
        # Manual button and auto-restart can race; never run two restarts at once
        if not self._restart_lock.acquire(blocking=False):
            return False, "Restart already in progress"
        try:
            return self._restart_broadcast_locked()
        except Exception as exc:
            return False, str(exc)
        finally:
            self._restart_lock.release()

    def _restart_broadcast_locked(self) -> tuple[bool, str]:
        if self._get_active_broadcast():
            return True, "Already live"

        live_stream = self._get_live_stream()
        if not live_stream:
            return False, "No liveStream found matching RTMP stream key"
        stream_id = live_stream["id"]

        # Check ingest before touching broadcasts, so a dead RTMP feed costs nothing more
        if live_stream.get("status", {}).get("streamStatus") != "active":
            return False, "Stream not active — is RTMP pushing?"

        # Reuse a broadcast already bound to our stream (left by an earlier attempt) — it carries the
        # user's title/description/settings, and YouTube rate-limits creating new broadcasts
        stale = self._get_stale_broadcasts(stream_id)
        if stale:
            broadcast_id = stale[0]["id"]
            title = stale[0].get("snippet", {}).get("title", "Live Stream")
            print(f"[youtube] Reusing broadcast {broadcast_id!r} ({title!r})", flush=True)
        else:
            last = self._get_last_broadcast()
            broadcast_id = self._create_broadcast(last)
            if not broadcast_id:
                return False, "Failed to create broadcast"
            title = (last or {}).get("snippet", {}).get("title", "Live Stream")
            try:
                self._bind_broadcast(broadcast_id, stream_id)
            except Exception:
                # Unbound, it wouldn't be found for reuse later
                self._delete_broadcast(broadcast_id)
                raise
            print(f"[youtube] Created broadcast {broadcast_id!r} ({title!r})", flush=True)

        usable = ("ready", "testStarting", "testing", "liveStarting", "live")
        status, item = self._wait_for_lifecycle(broadcast_id, usable, timeout_sec=15, interval_sec=1)
        if status not in usable:
            return False, f"Broadcast did not reach ready state (stuck in {status!r}) — try again in a moment"
        if status in ("liveStarting", "live"):
            print(f"[youtube] Broadcast {broadcast_id!r} already {status} ({title!r})", flush=True)
            self._mark_started(broadcast_id, title)
            return True, f"Broadcast already live: {title}"

        # With the monitor stream enabled (YouTube's default), ready -> live is an invalid transition;
        # the broadcast has to pass through testing first
        monitor = item.get("contentDetails", {}).get("monitorStream", {}).get("enableMonitorStream", True)
        if status == "ready" and monitor:
            try:
                self._transition(broadcast_id, "testing")
            except RuntimeError as exc:
                raise RuntimeError(f"{exc} (transition to testing; broadcast was {status!r})") from exc
            status = "testStarting"
        if status == "testStarting":
            status, _ = self._wait_for_lifecycle(broadcast_id, ("testing",), timeout_sec=60, interval_sec=3)
            if status != "testing":
                return False, f"Broadcast did not reach testing state (stuck in {status!r}) — try again in a moment"

        try:
            self._transition(broadcast_id, "live")
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} (transition to live; broadcast was {status!r})") from exc
        print(f"[youtube] Sent go-live for broadcast {broadcast_id!r} ({title!r}) — waiting for YouTube to confirm", flush=True)
        self._mark_started(broadcast_id, title)

        # Only now, with a broadcast live, clear older leftovers (capped — each delete costs 50 units)
        for other in stale[1:6]:
            self._delete_broadcast(other["id"])
        return True, f"Sent to YouTube — waiting for it to go live: {title}"

    def _mark_started(self, broadcast_id: str, title: str):
        """We just transitioned this broadcast: report "starting" (not live — YouTube hasn't confirmed
        yet) and poll fast until it does."""
        with self._lock:
            self._status = {
                "live": False, "starting": True, "broadcast_id": broadcast_id, "title": title,
                "stream_health": None, "stream_issues": [], "last_error": None,
                "started_at": None, "ended_at": self._status.get("ended_at"),
                "checked_at": self._status.get("checked_at"),
            }
            self._auto_restart_backoff = 0.0
            self._auto_restart_retry_at = 0.0
            self._expect_live = True
            self._fast_poll_until = time.monotonic() + _YT_FAST_POLL_WINDOW_SEC
        self._poll_now.set()  # restart the wait at the fast interval

    # ── Background poll ───────────────────────────────────────────────────────

    def _poll(self):
        try:
            active = self._get_active_broadcast()
            if active:
                snippet = active.get("snippet", {})
                stream_id = active.get("contentDetails", {}).get("boundStreamId")
                # Health is informational only; refresh it less often than the live check to save quota
                now = time.monotonic()
                if stream_id and (stream_id != self._last_health.get("_stream_id")
                                  or now - self._last_health_at >= _YT_HEALTH_POLL_SEC):
                    self._last_health = {**self._get_stream_health(stream_id), "_stream_id": stream_id}
                    self._last_health_at = now
                health = self._last_health if stream_id else {}
                with self._lock:
                    self._auto_restart_backoff = 0.0
                    self._auto_restart_retry_at = 0.0
                    if self._expect_live:
                        self._expect_live = None
                        self._fast_poll_until = 0.0
                    self._status = {
                        "live": True,
                        "starting": False,
                        "checked_at": _utc_iso(),
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
                    if self._expect_live and time.monotonic() < self._fast_poll_until:
                        # YouTube can lag a few seconds behind a transition we just made; stay "starting"
                        self._status["checked_at"] = _utc_iso()
                        return
                    timed_out = bool(self._expect_live)
                    self._expect_live = None
                    prev_live = self._status.get("live", False)
                    ended_at = _utc_iso() if prev_live else self._status.get("ended_at")
                    self._status = {
                        "live": False, "starting": False, "broadcast_id": None, "title": None,
                        "stream_health": None, "stream_issues": [],
                        "last_error": "Broadcast did not go live within 2 minutes of restarting" if timed_out else None,
                        "started_at": None, "ended_at": ended_at, "checked_at": _utc_iso(),
                    }
                    auto = self._auto_restart and time.monotonic() >= self._auto_restart_retry_at
                # Don't spend API calls restarting until we've been sending video long enough for
                # YouTube to see it (also covers addon startup, before the pipeline is up)
                if auto and (_manager_ref is None or _manager_ref.output_stable_sec < _YT_MIN_SENDING_SEC):
                    auto = False
                if auto:
                    print("[youtube] Broadcast not live, auto-restarting...", flush=True)
                    ok, msg = self.restart_broadcast()
                    print(f"[youtube] Auto-restart result: {msg}", flush=True)
                    with self._lock:
                        if ok:
                            self._auto_restart_backoff = 0.0
                            self._auto_restart_retry_at = 0.0
                        else:
                            # Back off 60s, 120s, ... capped at 5 min: a down broadcast is worse than a few API calls
                            self._auto_restart_backoff = min(max(self._auto_restart_backoff * 2, 60.0), 300.0)
                            self._auto_restart_retry_at = time.monotonic() + self._auto_restart_backoff
                            print(f"[youtube] Next auto-restart attempt in {self._auto_restart_backoff:.0f}s", flush=True)
        except Exception as exc:
            print(f"[youtube] Poll error: {exc}", flush=True)
            err_str = str(exc)
            is_auth_error = "No access token" in err_str or ": 401 " in err_str
            with self._lock:
                self._status["last_error"] = err_str
                if is_auth_error:
                    prev_live = self._status.get("live", False)
                    self._access_token = None
                    self._token_expires_at = 0.0
                    self._expect_live = None
                    self._fast_poll_until = 0.0
                    self._status.update({
                        "live": False, "starting": False, "broadcast_id": None, "title": None,
                        "stream_health": None, "stream_issues": [],
                        "started_at": None,
                        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if prev_live else self._status.get("ended_at"),
                    })

    def force_poll(self):
        self._poll_now.set()

    def run(self):
        while not self._stopping.is_set():
            cfg = _get_cfg()
            configured = cfg.get("youtube_refresh_token") and cfg.get("youtube_client_id") and cfg.get("youtube_client_secret")
            paused = self.quota_paused_until()
            if configured and paused:
                with self._lock:
                    resume = time.strftime("%H:%M", time.localtime(paused))
                    self._status["last_error"] = f"YouTube API quota exhausted — paused until {resume}"
            elif configured:
                self._poll()
            self._poll_now.clear()
            with self._lock:
                fast = time.monotonic() < self._fast_poll_until
            # Never skip restarts to save quota — when the budget is tight, poll status less instead
            budget = int(_get_cfg().get("youtube_daily_quota", _YT_DAILY_QUOTA_DEFAULT))
            tight = self.quota_used() > budget * _YT_SLOW_POLL_QUOTA_FRACTION
            interval = _YT_FAST_POLL_SEC if fast else _YT_SLOW_POLL_SEC if tight else _YT_POLL_SEC
            self.poll_interval_sec = interval
            self._poll_now.wait(interval)  # or wake early via force_poll() / stop()

    def stop(self):
        self._stopping.set()
        self._poll_now.set()  # break out of wait


# ---------------------------------------------------------------------------
# Snapshot state (main process only)
# ---------------------------------------------------------------------------

_snapshot_lock = threading.Lock()
_latest_snapshot: bytes | None = None
_snapshot_gen: int = 0                 # pipeline generation the snapshot came from
_snapshot_captured_at: float = 0.0     # epoch time it was captured

_video_clients_lock = threading.Lock()
_video_clients: list = []


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

# The page shell (web/index.html) and the UI bundle (web/dist/app.js, built from web/src/app.jsx by
# `npm run build` in the Docker image) are served from disk; nothing is loaded from a CDN.
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_WEB_BUNDLE = os.path.join(_WEB_DIR, "dist", "app.js")


def _webui_page(embed: bool):
    if not os.path.exists(_WEB_BUNDLE):
        return ("Web UI not built: run `npm ci && npm run build` in rtsp_switcher/web "
                "(the Docker image does this automatically).", 500, {"Content-Type": "text/plain; charset=utf-8"})
    with open(os.path.join(_WEB_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    ingress_path = request.headers.get("X-Ingress-Path", "")
    script = f"window.INGRESS_PATH={json.dumps(ingress_path)};" + ("window.EMBED_MODE=true;" if embed else "")
    html = html.replace("<!--INGRESS_PATH-->", f"<script>{script}</script>")
    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}

_flask_app = Flask(__name__)
# Ping websocket clients so a closed tab (or dead connection) is noticed even when no frames flow
_flask_app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 10}
_sock = _Sock(_flask_app)
_manager_ref = None
_ha_listener_ref = None
_youtube_manager_ref: YouTubeManager | None = None
_yt_device_flow_state: dict = {}  # device_code while auth is in progress
_SERVER_MANAGED_KEYS = ("youtube_refresh_token", "youtube_auto_restart")
# Never sent to the browser. GET blanks them and lists the ones that are set in _saved_secrets;
# a blank value in POST keeps the saved one. (The refresh token is server-managed and never sent.)
_SECRET_KEYS = ("ha_token", "youtube_client_secret")
_NEVER_SENT_KEYS = ("youtube_refresh_token",)

# Under the HA Supervisor, only HA's ingress proxy (which does HA's login) may use the web UI/API,
# not other containers on the hassio network. Not enforced when run outside HA (Dockerfile.local).
_INGRESS_PROXY_ADDR = "172.30.32.2"
_ENFORCE_INGRESS = bool(os.environ.get("SUPERVISOR_TOKEN"))


@_flask_app.before_request
def _only_from_ingress():
    if _ENFORCE_INGRESS and request.remote_addr not in (_INGRESS_PROXY_ADDR, "127.0.0.1", "::1"):
        abort(403)


@_flask_app.route("/")
def _webui_index():
    return _webui_page(embed=False)


@_flask_app.route("/embed")
def _webui_embed():
    return _webui_page(embed=True)


@_flask_app.route("/app.js")
def _webui_bundle():
    # Revalidated on every load (ETag), so an addon update is picked up immediately
    return send_file(_WEB_BUNDLE, mimetype="text/javascript", max_age=0)


@_flask_app.route("/api/config")
def _api_config_get():
    cfg = dict(_get_cfg())
    for key in _NEVER_SENT_KEYS:
        cfg.pop(key, None)
    cfg["_saved_secrets"] = [k for k in _SECRET_KEYS if cfg.get(k)]
    for key in _SECRET_KEYS:
        cfg[key] = ""
    return jsonify(cfg)


@_flask_app.route("/api/config", methods=["POST"])
def _api_config_post():
    new_cfg = request.get_json(force=True)
    if not isinstance(new_cfg, dict):
        return jsonify({"error": "invalid body"}), 400
    # Keys owned by dedicated endpoints — never taken from the client, whose copy may be stale
    current = _get_cfg()
    for key in _SERVER_MANAGED_KEYS:
        if key in current:
            new_cfg[key] = current[key]
        else:
            new_cfg.pop(key, None)
    new_cfg.pop("_saved_secrets", None)
    for key in _SECRET_KEYS:  # blank = keep the saved secret (the browser never has it)
        if not new_cfg.get(key):
            if current.get(key):
                new_cfg[key] = current[key]
            else:
                new_cfg.pop(key, None)
    for key, (lo, hi, default) in _NUMERIC_SETTINGS.items():
        if key not in new_cfg or new_cfg[key] is None:
            new_cfg[key] = current.get(key, default)
            continue
        v = new_cfg[key]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return jsonify({"error": f"{key} must be a whole number from {lo} to {hi}", "field": key}), 400

    m = _manager_ref
    on_air = m.current_stream if m else None
    new_on_air = on_air
    old_names = [st.get("stream_name") for st in current.get("streams", [])]
    new_names = [st.get("stream_name") for st in new_cfg.get("streams", [])]
    if on_air is not None and on_air not in new_names:
        # Renamed (same URL, or same position when only edits happened) or removed?
        old_streams, new_streams = current.get("streams", []), new_cfg.get("streams", [])
        old_active = next((st for st in old_streams if st.get("stream_name") == on_air), None)
        match = None
        if old_active is not None:
            match = next((st for st in new_streams if st.get("stream_url") == old_active.get("stream_url")), None)
            if match is None and len(new_streams) == len(old_streams):
                match = new_streams[old_streams.index(old_active)]
        if match is None:
            return jsonify({"error": f"{on_air} is on air — switch to another camera before removing it"}), 409
        new_on_air = match.get("stream_name")
    restart = _pipeline_affected(current, new_cfg, on_air, new_on_air)
    try:
        _save_config(new_cfg)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if m:
        if new_on_air != on_air:
            m.rename_stream(on_air, new_on_air)
        if restart:
            m.restart()
    if new_names != old_names and _ha_listener_ref:
        _ha_listener_ref.resync()
    return jsonify({"ok": True, "restarted": restart})


# (min, max, default) — values outside these break the pipeline, e.g. a 0s stall timeout restarts it every second
_NUMERIC_SETTINGS = {
    "rtsp_latency_ms": (0, 5000, 200),
    "reconnect_delay_sec": (1, 60, 2),
    "output_stall_timeout_sec": (5, 120, 10),
    "startup_output_timeout_sec": (10, 300, 20),
}


# Settings the running pipeline is built from; changing any of these (or the on-air camera's own
# settings) needs a pipeline restart, which briefly interrupts the YouTube stream
_PIPELINE_KEYS = (
    "rtmp_url", "output_width", "output_height", "output_framerate", "video_bitrate_kbps",
    "rtsp_latency_ms", "video_preview", "output_stall_timeout_sec", "startup_output_timeout_sec",
)


def _pipeline_affected(old_cfg: dict, new_cfg: dict, old_name: str | None, new_name: str | None) -> bool:
    if any(old_cfg.get(k) != new_cfg.get(k) for k in _PIPELINE_KEYS):
        return True

    def settings(cfg, name):
        st = next((x for x in cfg.get("streams", []) if x.get("stream_name") == name), None)
        return None if st is None else {k: v for k, v in st.items() if k != "stream_name"}

    return settings(old_cfg, old_name) != settings(new_cfg, new_name)


@_flask_app.route("/api/snapshot")
def _api_snapshot():
    with _snapshot_lock:
        data, gen = _latest_snapshot, _snapshot_gen
    # Never serve a frame from a camera other than the one currently selected
    m = _manager_ref
    if data is None or m is None or m.current_stream is None or m.stream_for_generation(gen) != m.current_stream:
        abort(503)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@_flask_app.route("/api/switch", methods=["POST"])
def _api_switch():
    data = request.get_json(force=True)
    name = (data or {}).get("name")
    if not name:
        return jsonify({"error": "name required"}), 400

    if name not in [st.get("stream_name") for st in _get_cfg().get("streams", [])]:
        return jsonify({"error": f"Unknown camera: {name}"}), 404

    # Switch here directly (HA's echo of the change is then a no-op) and mirror it to HA's input_select
    if _manager_ref:
        _manager_ref.switch_stream(name)
    if _ha_listener_ref:
        try:
            _ha_listener_ref.select_option(name)
        except Exception:
            pass  # already logged; the local switch has happened regardless
    return jsonify({"ok": True})


@_flask_app.route("/api/probe", methods=["POST"])
def _api_probe():
    url = ((request.get_json(force=True) or {}).get("url", "")).strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0",
             "-rtsp_transport", "tcp", url],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Stream did not respond within 15 seconds"}), 408
    except FileNotFoundError:
        return jsonify({"error": "ffprobe not available on server"}), 500
    if not result.stdout:
        return jsonify({"error": "Could not connect to stream"}), 400
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return jsonify({"error": "Could not parse stream info"}), 500
    if not streams:
        return jsonify({"error": "No video stream found in RTSP source"}), 400
    s = streams[0]
    codec = s.get("codec_name", "h264").lower()
    if codec == "hevc":
        codec = "h265"
    elif codec not in ("h264", "h265"):
        codec = "h264"
    width = int(s.get("width", 1920))
    height = int(s.get("height", 1080))
    fr_str = s.get("r_frame_rate") or s.get("avg_frame_rate", "30/1")
    try:
        num, den = fr_str.split("/")
        framerate = round(int(num) / max(int(den), 1))
    except Exception:
        framerate = 30
    return jsonify({"width": width, "height": height, "framerate": framerate, "codec": codec})


@_flask_app.route("/api/status")
def _api_status():
    m = _manager_ref
    stream = m.current_stream if m else None
    gen = m.pipeline_generation if m else 0
    with _snapshot_lock:
        snap_gen, captured_at = _snapshot_gen, _snapshot_captured_at
    snap_stream = m.stream_for_generation(snap_gen) if m else None
    # Only describe a snapshot that belongs to the selected camera; age is computed here so
    # browser clock skew can't make a stale frame look fresh
    snapshot = None
    if stream is not None and snap_stream == stream and captured_at:
        snapshot = {"stream": snap_stream, "id": captured_at, "age_sec": round(time.time() - captured_at, 1)}
    on_air, selected_state = m.camera_state() if m else (None, None)
    return jsonify({"active_stream": stream, "streaming": stream is not None,
                    "on_air_stream": on_air, "selected_state": selected_state,
                    "pipeline_generation": gen, "snapshot": snapshot})


@_flask_app.route("/api/youtube/status")
def _api_youtube_status():
    m = _youtube_manager_ref
    cfg = _get_cfg()
    configured = bool(cfg.get("youtube_refresh_token") and cfg.get("youtube_client_id") and cfg.get("youtube_client_secret"))
    status = m.get_status() if m else {"live": False, "broadcast_id": None, "title": None, "stream_health": None}
    status["configured"] = configured
    status["auto_restart"] = cfg.get("youtube_auto_restart", False)
    status["rtmp_sending"] = _manager_ref.output_active if _manager_ref else False
    status["poll_interval_sec"] = m.poll_interval_sec if m else _YT_POLL_SEC
    status["quota_used"] = m.quota_used() if m else 0
    status["quota_budget"] = int(cfg.get("youtube_daily_quota", _YT_DAILY_QUOTA_DEFAULT))
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


@_flask_app.route("/api/youtube/auth/revoke", methods=["POST"])
def _api_youtube_auth_revoke():
    new_cfg = dict(_get_cfg())
    new_cfg["youtube_refresh_token"] = ""
    _save_config(new_cfg)
    if _youtube_manager_ref:
        _youtube_manager_ref.reset_auth()
    return jsonify({"ok": True})


@_flask_app.route("/api/youtube/restart", methods=["POST"])
def _api_youtube_restart():
    m = _youtube_manager_ref
    if not m:
        return jsonify({"error": "YouTube manager not running"}), 503
    # YouTube can't go live without incoming video: refuse instead of spending quota on a restart
    # that fails ~2 minutes later and leaves the broadcast stuck in 'testing'
    if not (_manager_ref and _manager_ref.output_active):
        return jsonify({"ok": False, "message": "No video is being sent to YouTube — restart once the camera is streaming"}), 409
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


@_sock.route('/ws/video')
class _VideoClient(_queue_mod.Queue):
    """Per-browser frame queue. needs_key: skip delta frames until the next keyframe, set on connect
    and whenever a frame had to be dropped for this browser."""
    def __init__(self):
        super().__init__(maxsize=30)
        self.needs_key = True


def _ws_video(ws):
    client_q = _VideoClient()
    with _video_clients_lock:
        _video_clients.append(client_q)
    try:
        while True:
            try:
                msg = client_q.get(timeout=5)
                ws.send(msg)
            except _queue_mod.Empty:
                if not ws.connected:  # browser went away while no frames were flowing
                    break
    except Exception:
        pass
    finally:
        with _video_clients_lock:
            try:
                _video_clients.remove(client_q)
            except ValueError:
                pass


def _drain_video_frames(video_queue):
    """Daemon thread: reads H.264 frames from video_queue, broadcasts to all WS clients."""
    while True:
        try:
            generation, is_keyframe, pts_us, data = video_queue.get(timeout=5)
        except Exception:
            continue
        # Tag every frame with its pipeline generation and camera so the browser never labels
        # video with the wrong camera (looked up per frame so a rename applies immediately)
        m = _manager_ref
        name = ((m.stream_for_generation(generation) if m else None) or "").encode()[:65535]
        # Wire format: [1B: 0x01=keyframe/0x00=delta][8B: PTS µs][4B: generation][2B: name length]
        #              [name UTF-8][Annex-B H.264]  (integers big-endian)
        msg = ((b'\x01' if is_keyframe else b'\x00') + pts_us.to_bytes(8, 'big')
               + (generation & 0xFFFFFFFF).to_bytes(4, 'big') + len(name).to_bytes(2, 'big') + name + data)
        with _video_clients_lock:
            clients = list(_video_clients)
        for client in clients:
            if client.needs_key and not is_keyframe:
                continue
            try:
                client.put_nowait(msg)
                client.needs_key = False
            except _queue_mod.Full:
                # Browser can't keep up: discard its backlog (every queued delta depends on the one
                # before it) and restart it at the next keyframe; keep this frame if it is one
                while True:
                    try:
                        client.get_nowait()
                    except _queue_mod.Empty:
                        break
                client.needs_key = not is_keyframe
                if is_keyframe:
                    client.put_nowait(msg)


def _start_webserver():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    _flask_app.run(host="0.0.0.0", port=8099, debug=False, use_reloader=False, threaded=True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _quote_uri(uri):
    return '"' + uri.replace('"', '\\"') + '"'


def _preview_available(stream: dict, cfg: dict) -> bool:
    """The live preview branch is only built for H.264 cameras (the browser decodes H.264 only).
    Mirrored by previewAvailable() in the web UI."""
    return bool(cfg.get("video_preview", False)) and stream.get("stream_codec", "h264").lower() == "h264"


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

    w, h, fr = cfg["output_width"], cfg["output_height"], cfg["output_framerate"]

    # Add preview tee for H.264 streams when video_preview is enabled
    preview_enabled = _preview_available(stream, cfg)
    parse0_suffix = " ! tee name=t_h264" if preview_enabled else ""

    parts = [
        f"rtspsrc name=src0 location={_quote_uri(stream['stream_url'])} "
        f"protocols=tcp tcp-timeout=30000000 latency={cfg.get('rtsp_latency_ms', 200)} ! "
        "queue name=qsrc0 ! "
        f"{depay} name=depay0 ! {parser_in} name=parse0{parse0_suffix} ! "
        "queue name=preq0 ! "
        f"{decoder} name=dec0 ! "
        "videoconvert ! tee name=t ! "
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
    if preview_enabled:
        parts.append(
            # The leaky queue is the only place this branch drops frames (so it can never stall the
            # RTMP output); it flags the next buffer DISCONT, which _video_loop uses to resync
            "t_h264. ! queue name=prevq max-size-buffers=5 leaky=downstream ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=prevsink emit-signals=false max-buffers=30 drop=false"
        )
    return " ".join(parts)


def _snapshot_loop(pipeline, snapshot_queue, generation):
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
                snapshot_queue.put_nowait((generation, time.time(), data))
            except Exception:
                pass


def _video_loop(pipeline, video_queue, generation):
    """Runs as a daemon thread inside the worker process. Pulls H.264 frames from prevsink."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    prevsink = pipeline.get_by_name("prevsink")
    if prevsink is None:
        return  # preview not in this pipeline build

    # Delta frames only make sense after every frame before them, so after any dropped frame skip
    # ahead to the next keyframe: the browser freezes briefly instead of decoding a smeared picture
    needs_key = True
    while True:
        sample = prevsink.emit("try-pull-sample", Gst.SECOND // 2)
        if sample is None:
            continue
        buf = sample.get_buffer()
        is_keyframe = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        if buf.has_flags(Gst.BufferFlags.DISCONT):  # frames were dropped upstream (leaky prevq)
            needs_key = True
        if needs_key and not is_keyframe:
            continue
        pts_ns = buf.pts
        pts_us = pts_ns // 1000 if pts_ns != Gst.CLOCK_TIME_NONE else 0
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            continue
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        try:
            video_queue.put_nowait((generation, is_keyframe, pts_us, data))
            needs_key = False
        except Exception:
            needs_key = True  # queue full: this frame is lost, resync at the next keyframe


def pipeline_worker(stream: dict, cfg: dict, generation: int, snapshot_queue, video_queue,
                    last_output_shared, first_output_shared):
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
                last_output_shared.value = time.time()  # read by the main process for live UI status
                if not first_output_shared.value:
                    first_output_shared.value = last_output_shared.value
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

    threading.Thread(target=_snapshot_loop, args=(pipeline, snapshot_queue, generation), daemon=True).start()
    threading.Thread(target=_video_loop, args=(pipeline, video_queue, generation), daemon=True).start()

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
        self._resyncing = False

    def stop(self):
        self._stop.set()
        if self._client and hasattr(self._client, "__exit__"):
            try:
                self._client.__exit__(None, None, None)
            except Exception:
                pass

    def resync(self):
        """Camera names changed: reconnect, which re-syncs the input_select options on connect."""
        self._resyncing = True
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
            before = getattr(self._client.get_state(entity_id=cfg["ha_entity_id"]), "state", None)
        except Exception:
            before = None
        try:
            self._client.trigger_service(
                "input_select", "set_options",
                entity_id=cfg["ha_entity_id"],
                options=stream_names,
            )
            print(f"[ha] Set options on {cfg['ha_entity_id']}: {stream_names}", flush=True)
            # If HA's selection is no longer an option (e.g. the on-air camera was renamed), HA has just
            # jumped to the first option — put it back on the camera that's actually on air
            on_air = _manager_ref.current_stream if _manager_ref else None
            if before not in stream_names and on_air in stream_names:
                self._client.trigger_service(
                    "input_select", "select_option",
                    entity_id=cfg["ha_entity_id"],
                    option=on_air,
                )
                print(f"[ha] Re-selected {on_air!r} after options changed", flush=True)
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
                self._resyncing = False
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
                if self._resyncing:
                    self._resyncing = False
                    print("[ha] Reconnecting to sync camera list", flush=True)
                    continue
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
    def __init__(self, snapshot_queue, video_queue):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._current_stream = None
        self._process = None
        self._stopping = threading.Event()
        self._snapshot_queue = snapshot_queue
        self._video_queue = video_queue
        self._pipeline_generation = 0
        self._generation_streams: dict[int, str] = {}  # which camera each pipeline generation sent
        self._selected_at: float = 0.0          # epoch time the current camera was selected
        self._gen_started_at: float = 0.0       # epoch time the current pipeline generation started
        self._switched_from: str | None = None  # camera that was selected before it
        # Wall-clock time of the last encoded frame, written by the worker process
        self._last_output = multiprocessing.Value("d", 0.0, lock=False)
        # Wall-clock time of the first encoded frame from the current worker
        self._first_output = multiprocessing.Value("d", 0.0, lock=False)

    def camera_state(self) -> tuple[str | None, str | None]:
        """(camera whose video is being sent right now or None, state of the selected camera):
        "on_air", "switching" (no output yet after picking a different camera), "connecting"
        (addon startup), "reconnecting" (same camera dropped, < 10s), or "no_signal"."""
        with self._lock:
            current, gen = self._current_stream, self._pipeline_generation
            selected_at, switched_from = self._selected_at, self._switched_from
            gen_started_at = self._gen_started_at
            gen_stream = self._generation_streams.get(gen)
            prev_stream = self._generation_streams.get(gen - 1)
        on_air = gen_stream if self.output_active else None
        if current is None:
            return on_air, None
        if on_air == current:
            return on_air, "on_air"
        last = self._last_output.value
        # Pipelines run one at a time, so output older than this generation came from the previous one
        last_stream = gen_stream if last >= gen_started_at else prev_stream
        had_output = last_stream == current and last > selected_at
        waiting = time.time() - max(selected_at, last)
        if waiting >= 10:
            return on_air, "no_signal"
        if had_output:
            return on_air, "reconnecting"
        return on_air, "switching" if switched_from is not None else "connecting"

    @property
    def output_active(self) -> bool:
        """True if the pipeline produced output within the last few seconds (i.e. RTMP is being fed)."""
        last = self._last_output.value
        return last >= self._gen_started_at and time.time() - last < 3

    @property
    def output_stable_sec(self) -> float:
        """How long the current pipeline has been continuously producing output (0 if it isn't)."""
        first = self._first_output.value
        if not first or not self.output_active:
            return 0.0
        return time.time() - first

    @property
    def pipeline_generation(self):
        with self._lock:
            return self._pipeline_generation

    @property
    def current_stream(self):
        with self._lock:
            return self._current_stream

    def stream_for_generation(self, generation: int) -> str | None:
        with self._lock:
            return self._generation_streams.get(generation)

    def rename_stream(self, old: str, new: str):
        """The on-air camera was renamed: keep it selected under its new name, without a restart."""
        with self._lock:
            if self._current_stream == old:
                self._current_stream = new
            for gen, name in self._generation_streams.items():
                if name == old:
                    self._generation_streams[gen] = new
        print(f"[manager] Camera {old!r} renamed to {new!r}", flush=True)

    def switch_stream(self, name):
        cfg = _get_cfg()
        stream_names = [s["stream_name"] for s in cfg.get("streams", [])]
        if name not in stream_names:
            print(f"[manager] Unknown stream: {name!r} (known: {stream_names})", flush=True)
            return
        with self._lock:
            if self._current_stream == name:
                return
            self._switched_from = self._current_stream
            self._current_stream = name
            self._selected_at = time.time()
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
                        self._switched_from = None
                        self._current_stream = streams[0]["stream_name"]
                        self._selected_at = time.time()
                else:
                    self._stopping.wait(2)
                continue

            # Drain stale snapshots and video frames before starting new pipeline
            while True:
                try:
                    self._snapshot_queue.get_nowait()
                except Exception:
                    break
            while True:
                try:
                    self._video_queue.get_nowait()
                except Exception:
                    break

            self._first_output.value = 0.0
            with self._lock:
                self._pipeline_generation += 1
                generation = self._pipeline_generation
                # Output from a previous pipeline (older than this) never counts as this one's
                self._gen_started_at = time.time()
                self._generation_streams[generation] = stream_name
                for old_gen in [g for g in self._generation_streams if g < generation - 10]:
                    del self._generation_streams[old_gen]
            p = multiprocessing.Process(
                target=pipeline_worker,
                args=(stream, cfg, generation, self._snapshot_queue, self._video_queue,
                      self._last_output, self._first_output),
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
    video_queue = multiprocessing.Queue(maxsize=15)

    def _drain_snapshots():
        global _latest_snapshot, _snapshot_gen, _snapshot_captured_at
        while True:
            try:
                gen, captured_at, data = snapshot_queue.get(timeout=5)
                with _snapshot_lock:
                    _latest_snapshot, _snapshot_gen, _snapshot_captured_at = data, gen, captured_at
            except Exception:
                pass

    threading.Thread(target=_drain_snapshots, daemon=True).start()
    threading.Thread(target=_drain_video_frames, args=(video_queue,), daemon=True).start()

    global _manager_ref
    manager = PipelineManager(snapshot_queue, video_queue)
    _manager_ref = manager
    manager.start()

    print("[web] Accepting requests only from Home Assistant ingress" if _ENFORCE_INGRESS
          else "[web] Not running under Home Assistant — accepting requests from any address", flush=True)
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
