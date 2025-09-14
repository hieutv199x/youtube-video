import os
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Iterable
import webbrowser  # keep if still referenced elsewhere
import time
import ssl
import socket
import random  # added
import sys  # <-- added import (fix NameError for sys.platform in _CACHE_BASE)
from shutil import copy2  # new

from PyQt6.QtCore import QObject, pyqtSignal, QThread

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

# Legacy (project root) constants kept:
BASE_DIR = Path(__file__).parent.parent.parent
LEGACY_TOKEN_FILE = BASE_DIR / "token.json"
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"

def _user_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "YouTubeManager"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "YouTubeManager"
    return Path.home() / ".local" / "share" / "YouTubeManager"

_USER_DATA_DIR = _user_data_dir()
_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = _USER_DATA_DIR / "token.json"  # NEW canonical token location

def _iso_time_hours_ago(hours: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.isoformat().replace("+00:00", "Z")

def _chunk(iterable: Iterable, size: int):
    buf = []
    for item in iterable:
        buf.append(item)
        if len(buf) == size:
            yield buf
            buf = []
    if buf:
        yield buf

class _Worker(QThread):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.result = None
        self.error = None
    def run(self):
        try:
            self.result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.error = e

# ---------------- CACHING SUPPORT ----------------
_CACHE_BASE = (Path.home() / ("Library/Application Support" if os.name == "posix" and sys.platform == "darwin" else
               ("AppData/Local" if os.name == "nt" else ".local/share")) / "YouTubeManager" / "cache")
_CACHE_BASE.mkdir(parents=True, exist_ok=True)

_SUBS_CACHE_FILE = _CACHE_BASE / "subscriptions.json"
_VIDEOS_CACHE_DIR = _CACHE_BASE / "videos"
_PLAYLIST_CACHE_FILE = _CACHE_BASE / "playlists.json"
_VIDEOS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_SUBS_TTL_SEC = 6 * 3600
_VIDEOS_TTL_SEC = 2 * 3600

def _read_json(p: Path):
    try:
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _write_json(p: Path, data):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _is_fresh(p: Path, ttl: int) -> bool:
    return p.exists() and (time.time() - p.stat().st_mtime) < ttl

def _video_cache_path(channel_id: str) -> Path:
    return _VIDEOS_CACHE_DIR / f"{channel_id}.json"

# ------------- RETRY WITH EXPONENTIAL BACKOFF & JITTER -------------
def _execute_with_retries(request, retries: int = 5, base_delay: float = 1.0, max_delay: float = 16.0):
    """
    Execute a googleapiclient request with retry for transient / rate limit errors.
    Exponential backoff + jitter. QuotaExceeded is not retried aggressively
    (single retry) to avoid burning further quota quickly.
    """
    for attempt in range(1, retries + 1):
        try:
            return request.execute()
        except Exception as e:
            msg = str(e)
            lower = msg.lower()
            quota = "quotaexceeded" in lower
            rate_limited = ("ratelimitexceeded" in lower or "userRateLimitExceeded".lower() in lower)
            transient = any(k in lower for k in ["ssl", "timeout", "reset", "temporarily", "backenderror"])
            if attempt == retries or (quota and attempt >= 2):
                raise
            if not (quota or rate_limited or transient):
                raise
            # backoff
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = delay * (0.7 + random.random() * 0.6)  # jitter 70%-130%
            time.sleep(delay)

class YouTubeChannelService(QObject):
    auth_changed = pyqtSignal(bool)
    subscriptions_loaded = pyqtSignal(list)
    channel_videos_loaded = pyqtSignal(str, list)
    multiple_videos_loaded = pyqtSignal(list)  # aggregated: list of video dicts
    error_occurred = pyqtSignal(str)
    quota_exceeded = pyqtSignal(str, list)  # message, partial video list

    def __init__(self):
        super().__init__()
        # ...existing init fields...
        self._creds = None
        self._youtube = None
        self.last_error = None
        self._playlist_cache = _read_json(_PLAYLIST_CACHE_FILE) or {}
        self._migrate_legacy_token()
        self._silent_restore()

    def _migrate_legacy_token(self):
        """Copy legacy token.json (project root) to user data dir once."""
        try:
            if LEGACY_TOKEN_FILE.exists() and not TOKEN_FILE.exists():
                TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                copy2(LEGACY_TOKEN_FILE, TOKEN_FILE)
        except Exception:
            pass

    # ---------------- AUTH RESTORE ----------------
    def _silent_restore(self):
        try:
            if TOKEN_FILE.exists():
                creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except Exception:
                        pass
                if creds and creds.valid:
                    self._creds = creds
                    self._youtube = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
                    self.auth_changed.emit(True)
        except Exception as e:
            self.last_error = str(e)

    def ensure_session(self):
        """
        Ensure a valid authenticated session.
        Returns True if credentials are valid after (re)loading, else False.
        """
        if self._creds and self._creds.valid:
            # Emit in case UI created before auth_changed connected elsewhere
            self.auth_changed.emit(True)
            return True
        self._silent_restore()
        return bool(self._creds and self._creds.valid)

    # ---------------- INTERACTIVE AUTH (RESTORED) ----------------
    def authenticate(self, force: bool = False):
        """
        Start (or reuse) OAuth desktop flow.
        - Uses existing token.json silently if valid (unless force=True).
        - Refreshes if expired and refresh_token present.
        - Falls back to browser OAuth if needed.
        """
        if self._creds and self._creds.valid and not force:
            # Already authenticated
            self.auth_changed.emit(True)
            return

        def _do_auth():
            try:
                creds = None
                if TOKEN_FILE.exists():
                    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
                    if creds and creds.expired and creds.refresh_token:
                        try:
                            creds.refresh(Request())
                        except Exception:
                            pass
                need_browser = force or (not creds or not creds.valid)
                if need_browser:
                    if not CLIENT_SECRET_FILE.exists():
                        raise FileNotFoundError(
                            f"Missing client_secret.json at {CLIENT_SECRET_FILE}. "
                            "Download it from Google Cloud Console (OAuth Client ID - Desktop)."
                        )
                    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
                    creds = flow.run_local_server(port=0, open_browser=True)
                    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                        f.write(creds.to_json())
                    # also update legacy for backward compatibility
                    try:
                        with open(LEGACY_TOKEN_FILE, "w", encoding="utf-8") as lf:
                            lf.write(creds.to_json())
                    except Exception:
                        pass
                if not creds or not creds.valid:
                    raise RuntimeError("Failed to obtain valid credentials.")
                return creds
            except Exception as e:
                self.last_error = str(e)
                raise

        worker = _Worker(_do_auth)
        worker.finished.connect(lambda: self._after_auth(worker))
        worker.start()

    def _after_auth(self, worker: _Worker):
        if worker.error:
            self.last_error = str(worker.error)
            self.error_occurred.emit(self.last_error)
            self.auth_changed.emit(False)
            return
        self._creds = worker.result
        try:
            self._youtube = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
        except Exception as e:
            self.last_error = f"Failed to build YouTube client: {e}"
            self.error_occurred.emit(self.last_error)
            self.auth_changed.emit(False)
            return
        self.auth_changed.emit(True)

    # ---------------- SUBSCRIPTIONS (CACHED) ----------------
    def load_subscriptions(self, max_channels: int = 500, use_cache: bool = True, force: bool = False):
        if not self._creds or not self._creds.valid:
            self.error_occurred.emit("Not authenticated.")
            return
        if use_cache and not force and _is_fresh(_SUBS_CACHE_FILE, _SUBS_TTL_SEC):
            cached = _read_json(_SUBS_CACHE_FILE)
            if cached:
                self.subscriptions_loaded.emit(cached)
                return
        def _subs():
            subs = []
            token = None
            if not self._youtube:
                self._youtube = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
            while len(subs) < max_channels:
                resp = _execute_with_retries(
                    self._youtube.subscriptions().list(
                        part="snippet",
                        mine=True,
                        maxResults=min(50, max_channels - len(subs)),
                        pageToken=token,
                        order="alphabetical",
                        fields="items(snippet/resourceId/channelId,snippet/title),nextPageToken"
                    )
                )
                for item in resp.get("items", []):
                    subs.append({
                        "channel_id": item["snippet"]["resourceId"]["channelId"],
                        "title": item["snippet"]["title"]
                    })
                token = resp.get("nextPageToken")
                if not token:
                    break
            _write_json(_SUBS_CACHE_FILE, subs)
            return subs
        worker = _Worker(_subs)
        worker.finished.connect(lambda: self._emit_subs(worker))
        worker.start()

    # ---------------- PLAYLIST RESOLUTION (CACHED) ----------------
    def _batch_resolve_playlists(self, svc, channel_ids: list[str]):
        missing = [cid for cid in channel_ids if cid not in self._playlist_cache]
        if not missing:
            return
        for group in _chunk(missing, 50):
            resp = _execute_with_retries(
                svc.channels().list(
                    part="contentDetails",
                    id=",".join(group),
                    fields="items(id,contentDetails/relatedPlaylists/uploads)"
                )
            )
            for item in resp.get("items", []):
                cid = item.get("id")
                uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                if cid and uploads:
                    self._playlist_cache[cid] = uploads
        _write_json(_PLAYLIST_CACHE_FILE, self._playlist_cache)

    # ---------------- CHANNEL VIDEOS (CACHED, PLAYLIST-FIRST) ----------------
    def load_channel_videos(self, channel_id: str, max_results: int = 10, since_hours: int = 72,
                            use_cache: bool = False, force: bool = True, use_search_fallback: bool = False):
        # defaults changed: use_cache False, force True
        if not self._creds:
            self.error_occurred.emit("Not authenticated.")
            return
        cutoff_iso = _iso_time_hours_ago(since_hours)
        cache_path = _video_cache_path(channel_id)
        if use_cache and not force and _is_fresh(cache_path, _VIDEOS_TTL_SEC):
            cached = _read_json(cache_path) or []
            filtered = [v for v in cached if v.get("published_at", "") >= cutoff_iso]
            self.channel_videos_loaded.emit(channel_id, filtered[:max_results])
            return

        def _videos():
            svc = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
            self._batch_resolve_playlists(svc, [channel_id])
            pl_id = self._playlist_cache.get(channel_id)
            vids = []
            if pl_id:
                vids = self._fetch_playlist_recent(svc, pl_id, channel_id, cutoff_iso, max_results)
            if not vids and use_search_fallback:
                # fallback (expensive)
                vids = self._search_channel_recent_videos(svc, channel_id, cutoff_iso, max_results)
            # merge with existing cached (keep superset for future narrower cutoff)
            existing = _read_json(cache_path) or []
            existing_map = {v.get("video_id"): v for v in existing}
            for v in vids:
                existing_map[v["video_id"]] = v
            merged = list(existing_map.values())
            merged.sort(key=lambda x: x.get("published_at", ""), reverse=True)
            _write_json(cache_path, merged)
            # return only filtered & limited
            return [v for v in merged if v.get("published_at", "") >= cutoff_iso][:max_results]

        worker = _Worker(_videos)
        worker.finished.connect(lambda: self._emit_videos(worker, channel_id))
        worker.start()

    def _fetch_playlist_recent(self, svc, playlist_id: str, channel_id: str,
                               cutoff_iso: str, max_results: int) -> list:
        videos = []
        page_token = None
        while len(videos) < max_results:
            resp = _execute_with_retries(
                svc.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=playlist_id,
                    maxResults=min(50, max_results - len(videos)),
                    pageToken=page_token,
                    fields="items(snippet/publishedAt,snippet/title,snippet/resourceId/videoId),nextPageToken"
                )
            )
            older_only = True
            for it in resp.get("items", []):
                sn = it["snippet"]
                pub = sn["publishedAt"]
                if pub < cutoff_iso:
                    continue  # skip older (do not count)
                older_only = False
                vid = sn["resourceId"]["videoId"]
                videos.append({
                    "video_id": vid,
                    "title": sn["title"],
                    "published_at": pub,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "_channel_id": channel_id
                })
                if len(videos) >= max_results:
                    break
            if len(videos) >= max_results:
                break
            page_token = resp.get("nextPageToken")
            # Stop if no more pages or page had only older content
            if not page_token or older_only:
                break
        return videos

    # (Retain search method but not default)
    def _search_channel_recent_videos(self, svc, channel_id: str, published_after_iso: str,
                                      max_results: int) -> list:
        """
        Use search.list (fewer calls) to get recent videos after published_after_iso.
        Returns list[{video_id,title,published_at,url}]
        """
        videos = []
        page_token = None
        while len(videos) < max_results:
            resp = _execute_with_retries(
                svc.search().list(
                    part="snippet",
                    channelId=channel_id,
                    publishedAfter=published_after_iso,
                    order="date",
                    type="video",
                    maxResults=min(50, max_results - len(videos)),
                    pageToken=page_token,
                    fields="items(id/videoId,snippet/publishedAt,snippet/title),nextPageToken"
                )
            )
            if resp is not None:
                for item in resp.get("items", []):
                    vid = item["id"]["videoId"]
                    sn = item["snippet"]
                    videos.append({
                        "video_id": vid,
                        "title": sn["title"],
                        "published_at": sn["publishedAt"],
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "_channel_id": channel_id
                    })
            page_token = resp.get("nextPageToken") if resp is not None else None
            if not page_token:
                break
        return videos

    # ------------------ MODIFIED: load_multiple_channels_videos ------------------
    def load_multiple_channels_videos(self, channel_ids: list, max_results: int, since_hours: int,
                                      use_search_strategy: bool = False, channel_limit: int | None = None,
                                      use_cache: bool = False):
        # default use_cache now False
        """Aggregate newest videos across channels efficiently."""
        if not self._creds:
            self.error_occurred.emit("Not authenticated.")
            return
        if channel_limit:
            channel_ids = channel_ids[:channel_limit]
        cutoff_iso = _iso_time_hours_ago(since_hours)

        def _agg():
            svc = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
            self._batch_resolve_playlists(svc, channel_ids)
            aggregated = []
            quota_hit = False
            quota_msg = ""
            for cid in channel_ids:
                if quota_hit:
                    break
                try:
                    cache_path = _video_cache_path(cid)
                    cached = _read_json(cache_path) if (use_cache and _is_fresh(cache_path, _VIDEOS_TTL_SEC)) else None
                    vids = []
                    if cached:
                        vids = [v for v in cached if v.get("published_at", "") >= cutoff_iso][:max_results]
                    else:
                        pl = self._playlist_cache.get(cid)
                        if pl:
                            vids = self._fetch_playlist_recent(svc, pl, cid, cutoff_iso, max_results)
                        if not vids and use_search_strategy:
                            # fallback to search only if explicitly requested
                            vids = self._search_channel_recent_videos(svc, cid, cutoff_iso, max_results)
                        if vids:
                            # Merge & persist
                            existing = _read_json(cache_path) or []
                            emap = {v["video_id"]: v for v in existing}
                            for v in vids:
                                emap[v["video_id"]] = v
                            merged = list(emap.values())
                            merged.sort(key=lambda x: x.get("published_at", ""), reverse=True)
                            _write_json(cache_path, merged)
                    aggregated.extend(vids)
                except Exception as e:
                    emsg = str(e).lower()
                    if "quotaexceeded" in emsg:
                        quota_hit = True
                        quota_msg = "YouTube Data API quota exceeded. Partial results shown."
                        break
                    continue
            return {"videos": aggregated, "quota_hit": quota_hit, "quota_msg": quota_msg}

        worker = _Worker(_agg)
        worker.finished.connect(lambda: self._emit_multiple_with_quota(worker))
        worker.start()

    def _emit_multiple_with_quota(self, worker: '_Worker'):
        if worker.error:
            # propagate as normal error
            self.error_occurred.emit(str(worker.error))
            return
        data = worker.result or {}
        vids = data.get("videos", [])
        if data.get("quota_hit"):
            self.quota_exceeded.emit(data.get("quota_msg", "Quota exceeded"), vids)
        else:
            self.multiple_videos_loaded.emit(vids)

    def _emit_subs(self, worker: _Worker):
        if worker.error:
            self.error_occurred.emit(str(worker.error))
        else:
            self.subscriptions_loaded.emit(worker.result)

    def _emit_videos(self, worker: _Worker, channel_id: str):
        if worker.error:
            self.error_occurred.emit(str(worker.error))
        else:
            self.channel_videos_loaded.emit(channel_id, worker.result)

    # NOTE: quota helper (informational only; API does not expose remaining quota)
    @staticmethod
    def estimate_quota_units(channel_count, use_search_strategy):
        """
        Rough quota estimate:
          search.list cost ~100 units per channel (part=snippet).
          playlist path: channels.list(contentDetails) ~1 + first playlistItems.list page ~1 ≈2 units/channel.
        """
        per = 100 if use_search_strategy else 2
        return channel_count * per
