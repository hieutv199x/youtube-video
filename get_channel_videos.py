import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# Scopes: read-only access to YouTube account (subscriptions etc.)
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / "token.json"
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"  # Download from Google Cloud Console
CACHE_FILE = BASE_DIR / "seen_videos.json"

def get_authenticated_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET_FILE.exists():
                raise FileNotFoundError(
                    "client_secret.json not found. Download OAuth client credentials and place it next to this script."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def load_seen_cache() -> Dict[str, List[str]]:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}

def save_seen_cache(cache: Dict[str, List[str]]):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def list_subscriptions(youtube, max_channels: int = 50) -> List[Dict]:
    subs = []
    page_token = None
    while len(subs) < max_channels:
        resp = youtube.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=min(50, max_channels - len(subs)),
            pageToken=page_token,
            order="alphabetical"
        ).execute()
        for item in resp.get("items", []):
            subs.append({
                "channel_id": item["snippet"]["resourceId"]["channelId"],
                "title": item["snippet"]["title"]
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return subs

def get_uploads_playlist_id(youtube, channel_id: str) -> Optional[str]:
    resp = youtube.channels().list(
        part="contentDetails",
        id=channel_id
    ).execute()
    items = resp.get("items")
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

def fetch_videos_from_playlist(youtube, playlist_id: str, max_results: int = 20,
                               published_after: Optional[str] = None) -> List[Dict]:
    videos = []
    page_token = None
    while len(videos) < max_results:
        kwargs = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": min(50, max_results - len(videos)),
            "pageToken": page_token
        }
        resp = youtube.playlistItems().list(**kwargs).execute()
        for item in resp.get("items", []):
            snippet = item["snippet"]
            published_at = snippet["publishedAt"]
            if published_after and published_at <= published_after:
                continue
            vid = snippet["resourceId"]["videoId"]
            videos.append({
                "video_id": vid,
                "title": snippet["title"],
                "published_at": published_at,
                "url": f"https://www.youtube.com/watch?v={vid}"
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return videos

def filter_new_videos(channel_id: str, videos: List[Dict], cache: Dict[str, List[str]]) -> List[Dict]:
    seen = set(cache.get(channel_id, []))
    fresh = [v for v in videos if v["video_id"] not in seen]
    if fresh:
        # Update cache
        cache.setdefault(channel_id, [])
        cache[channel_id].extend([v["video_id"] for v in fresh])
    return fresh

def iso_time_hours_ago(hours: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.isoformat().replace("+00:00", "Z")

def process_single_channel(youtube, channel_id: str, max_results: int, published_after: Optional[str],
                           cache: Dict[str, List[str]]) -> List[Dict]:
    uploads = get_uploads_playlist_id(youtube, channel_id)
    if not uploads:
        print(f"Channel {channel_id} not found or no uploads playlist.")
        return []
    vids = fetch_videos_from_playlist(youtube, uploads, max_results=max_results, published_after=published_after)
    new_vids = filter_new_videos(channel_id, vids, cache)
    return new_vids

def main():
    parser = argparse.ArgumentParser(description="Fetch new videos from subscribed channels.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--channel", help="Specific channel ID to check.")
    group.add_argument("--all-subs", action="store_true", help="Process all subscriptions.")
    parser.add_argument("--max-channels", type=int, default=25, help="Max number of subscribed channels to scan.")
    parser.add_argument("--max-results", type=int, default=10, help="Max videos per channel to fetch.")
    parser.add_argument("--since-hours", type=int, default=72, help="Only videos published in last N hours.")
    parser.add_argument("--reset-cache", action="store_true", help="Ignore and clear seen_videos cache.")
    args = parser.parse_args()

    if args.reset_cache and CACHE_FILE.exists():
        CACHE_FILE.unlink()

    cache = load_seen_cache()

    try:
        youtube = get_authenticated_service()
    except Exception as e:
        print(f"Auth failed: {e}")
        return

    published_after = iso_time_hours_ago(args.since_hours) if args.since_hours else None

    results = []

    if args.channel:
        new_v = process_single_channel(youtube, args.channel, args.max_results, published_after, cache)
        if new_v:
            results.append({"channel_id": args.channel, "videos": new_v})
    else:
        subs = list_subscriptions(youtube, max_channels=args.max_channels)
        for sub in subs:
            new_v = process_single_channel(
                youtube,
                sub["channel_id"],
                args.max_results,
                published_after,
                cache
            )
            if new_v:
                results.append({"channel_id": sub["channel_id"], "channel_title": sub["title"], "videos": new_v})

    if results:
        print("New videos found:")
        for entry in results:
            title = entry.get("channel_title", entry["channel_id"])
            print(f"\nChannel: {title}")
            for v in entry["videos"]:
                print(f"- {v['published_at']} | {v['title']} | {v['url']}")
    else:
        print("No new videos found.")

    save_seen_cache(cache)

if __name__ == "__main__":
    main()
