import os
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Iterable
import webbrowser  # keep if still referenced elsewhere
import time
import ssl
import socket

from PyQt6.QtCore import QObject, pyqtSignal, QThread

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

BASE_DIR = Path(__file__).parent.parent.parent
TOKEN_FILE = BASE_DIR / "token.json"
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"

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

def _execute_with_retries(request, retries: int = 3, delay: float = 1.5):
    """
    Execute a googleapiclient request with simple retry for transient SSL/network errors.
    """
    for attempt in range(1, retries + 1):
        try:
            return request.execute()
        except Exception as e:
            msg = str(e)
            transient = (
                isinstance(e, (ssl.SSLError, socket.timeout, OSError))
                or "SSL" in msg
                or "ssl" in msg
                or "EOF occurred" in msg
                or "record layer" in msg
            )
            if not transient or attempt == retries:
                raise
            time.sleep(delay * attempt)

class YouTubeChannelService(QObject):
    auth_changed = pyqtSignal(bool)
    subscriptions_loaded = pyqtSignal(list)
    channel_videos_loaded = pyqtSignal(str, list)
    multiple_videos_loaded = pyqtSignal(list)  # aggregated: list of video dicts
    error_occurred = pyqtSignal(str)
    quota_exceeded = pyqtSignal(str, list)  # message, partial video list

    def __init__(self):
        super().__init__()
        self._creds = None
        self._youtube = None
        self.last_error = None  # store last error message
        self._playlist_cache: Dict[str, str] = {}  # channel_id -> uploads playlist id

    def is_authenticated(self) -> bool:
        return self._creds is not None and self._creds.valid

    def authenticate(self):
        """
        Begin OAuth.

        FAQ:
        - Do I always need to access Google Cloud Console? -> No.
          You use Google Cloud Console ONLY ONCE to create the OAuth Desktop Client
          and download client_secret.json. After that:
            * First run: Browser opens, you grant access, token.json created.
            * Subsequent runs: If token.json valid -> no browser, silent.
            * If token expired but has refresh_token -> auto refresh, no browser.
            * Browser shows again ONLY if:
                - token.json deleted/corrupted
                - refresh_token revoked
                - scopes changed
        - Can I avoid OAuth entirely? Only if you do NOT need private data
          (e.g., listing *your* subscriptions requires OAuth). For just fetching
          public videos of a known channel you could use an API key instead.
        """
        self.last_error = None
        def _auth():
            try:
                creds = None
                if TOKEN_FILE.exists():
                    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
                if creds and creds.valid:
                    return creds
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    return creds
                if not CLIENT_SECRET_FILE.exists():
                    raise FileNotFoundError(
                        f"client_secret.json not found at: {CLIENT_SECRET_FILE}. "
                        "Download once from Google Cloud Console (OAuth Desktop Client)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
                creds = flow.run_local_server(port=0, open_browser=True)
                with open(TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
                return creds
            except Exception as e:
                self.last_error = str(e)
                raise
        worker = _Worker(_auth)
        worker.finished.connect(lambda: self._after_auth(worker))
        worker.start()

    def _after_auth(self, worker: '_Worker'):
        if worker.error:
            self.last_error = str(worker.error)
            self.error_occurred.emit(self.last_error)
            self.auth_changed.emit(False)
            return
        self._creds = worker.result
        try:
            # build with cache disabled (avoids shared cache race)
            self._youtube = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
        except Exception as e:
            self.last_error = f"Failed to build YouTube client: {e}"
            self.error_occurred.emit(self.last_error)
            self.auth_changed.emit(False)
            return
        self.auth_changed.emit(True)

    def load_subscriptions(self, max_channels: int = 50):
        if not self._creds or not self._creds.valid:
            self.error_occurred.emit("Not authenticated.")
            return
        def _subs():
            subs = []
            token = None
            # Ensure self._youtube is initialized
            if not self._youtube:
                self._youtube = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
            while len(subs) < max_channels:
                resp = _execute_with_retries(
                    self._youtube.subscriptions().list(
                        part="snippet",
                        mine=True,
                        maxResults=min(50, max_channels - len(subs)),
                        pageToken=token,
                        order="alphabetical"
                    )
                )
                if resp is not None:
                    for item in resp.get("items", []):
                        subs.append({
                            "channel_id": item["snippet"]["resourceId"]["channelId"],
                            "title": item["snippet"]["title"]
                        })
                token = resp.get("nextPageToken") if resp is not None else None
                if not token:
                    break
            return subs
        worker = _Worker(_subs)
        worker.finished.connect(lambda: self._emit_subs(worker))
        worker.start()

    def _emit_subs(self, worker: _Worker):
        if worker.error:
            self.error_occurred.emit(str(worker.error))
        else:
            self.subscriptions_loaded.emit(worker.result)

    def load_channel_videos(self, channel_id: str, max_results: int = 10, since_hours: int = 72):
        if not self._creds:
            self.error_occurred.emit("Not authenticated.")
            return

        def _videos():
            svc = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
            published_after = _iso_time_hours_ago(since_hours)
            # Try search first
            vids = self._search_channel_recent_videos(svc, channel_id, published_after, max_results)
            if not vids:
                # Fallback to playlist
                self._batch_resolve_playlists(svc, [channel_id])
                pl_id = self._playlist_cache.get(channel_id)
                if pl_id:
                    vids = self._playlist_recent_videos(svc, pl_id, channel_id, published_after, max_results)
            return vids

        worker = _Worker(_videos)
        worker.finished.connect(lambda: self._emit_videos(worker, channel_id))
        worker.start()

    def _emit_videos(self, worker: _Worker, channel_id: str):
        if worker.error:
            self.error_occurred.emit(str(worker.error))
        else:
            self.channel_videos_loaded.emit(channel_id, worker.result)

    # ------------------ NEW HELPER METHODS ------------------

    def _batch_resolve_playlists(self, svc, channel_ids: list):
        """Resolve uploads playlist IDs for channel_ids not yet in cache using batched channels().list calls."""
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
            if resp is not None:
                for item in resp.get("items", []):
                    cid = item.get("id")
                    uploads = item["contentDetails"]["relatedPlaylists"]["uploads"]
                    self._playlist_cache[cid] = uploads

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

    def _playlist_recent_videos(self, svc, playlist_id: str, channel_id: str,
                                published_after_iso: str, max_results: int) -> list:
        """Fallback playlist-based retrieval with early stop."""
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
            page_oldest = True  # assume until we see a newer one
            if resp is not None:
                for it in resp.get("items", []):
                    sn = it["snippet"]
                    pub = sn["publishedAt"]
                    if pub <= published_after_iso:
                        # older than cutoff; skip but can consider stopping if many older
                        continue
                    page_oldest = False
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
            page_token = resp.get("nextPageToken") if resp is not None else None
            if not page_token or page_oldest:
                break
        return videos

    # ------------------ MODIFIED: load_multiple_channels_videos ------------------
    def load_multiple_channels_videos(self, channel_ids: list, max_results: int, since_hours: int,
                                      use_search_strategy: bool = True, channel_limit: int | None = None):
        """Aggregate newest videos across channels efficiently."""
        if not self._creds:
            self.error_occurred.emit("Not authenticated.")
            return
        if channel_limit:
            channel_ids = channel_ids[:channel_limit]

        def _agg():
            svc = build("youtube", "v3", credentials=self._creds, cache_discovery=False)
            published_after = _iso_time_hours_ago(since_hours)
            all_vids = []
            quota_hit = False
            quota_msg = ""
            for cid in channel_ids:
                if quota_hit:
                    break
                try:
                    channel_videos = []
                    if use_search_strategy:
                        channel_videos = self._search_channel_recent_videos(svc, cid, published_after, max_results)
                        if not channel_videos:
                            self._batch_resolve_playlists(svc, [cid])
                            pl = self._playlist_cache.get(cid)
                            if pl:
                                channel_videos = self._playlist_recent_videos(svc, pl, cid, published_after, max_results)
                    else:
                        self._batch_resolve_playlists(svc, [cid])
                        pl = self._playlist_cache.get(cid)
                        if pl:
                            channel_videos = self._playlist_recent_videos(svc, pl, cid, published_after, max_results)
                    all_vids.extend(channel_videos)
                except Exception as e:
                    emsg = str(e)
                    if "quotaExceeded" in emsg or "quotaexceeded" in emsg.lower():
                        quota_hit = True
                        quota_msg = "YouTube Data API quota exceeded. Partial results shown."
                        break
                    # ignore other per-channel errors
                    continue
            return {"videos": all_vids, "quota_hit": quota_hit, "quota_msg": quota_msg}

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
