"""
YouTube Inactive Subscriptions Finder
======================================
Lists all subscribed channels that haven't uploaded in the last 3 years.

Setup:
  1. Go to https://console.cloud.google.com/
  2. Create a project (or use existing)
  3. Enable "YouTube Data API v3"
  4. Create OAuth 2.0 credentials (Desktop app type)
  5. Download the JSON → save as `client_secret.json` in same directory as this script
  6. pip install google-api-python-client google-auth-oauthlib
  7. python youtube_inactive_subs.py

First run opens a browser for OAuth consent. Token is cached in `token.json` for reuse.
Output: `inactive_channels.csv`
"""

import csv
import os
import sys
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
CUTOFF_DATE = datetime(2023, 3, 18, tzinfo=timezone.utc)
CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"
OUTPUT_FILE = "inactive_channels.csv"


def authenticate():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_FILE):
                print(f"ERROR: '{CLIENT_SECRET_FILE}' not found in current directory.")
                print("Download it from Google Cloud Console → APIs & Services → Credentials")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def get_all_subscriptions(youtube):
    """Fetch all subscriptions, paginated."""
    subs = []
    page_token = None
    while True:
        resp = youtube.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=page_token,
            order="alphabetical",
        ).execute()
        for item in resp.get("items", []):
            subs.append({
                "subscription_id": item["id"],
                "channel_id": item["snippet"]["resourceId"]["channelId"],
                "channel_title": item["snippet"]["title"],
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return subs


def get_last_upload_date(youtube, channel_id):
    """
    Returns (datetime | None, str).
    None means no uploads found. str is ISO date or 'No uploads'.
    """
    try:
        # Get uploads playlist ID
        ch_resp = youtube.channels().list(
            part="contentDetails",
            id=channel_id,
        ).execute()
        items = ch_resp.get("items", [])
        if not items:
            return None, "Channel not found"

        uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        # Get most recent upload
        pl_resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist,
            maxResults=1,
        ).execute()
        pl_items = pl_resp.get("items", [])
        if not pl_items:
            return None, "No uploads"

        published = pl_items[0]["contentDetails"]["videoPublishedAt"]
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        return dt, dt.strftime("%Y-%m-%d")

    except HttpError as e:
        if e.resp.status == 404:
            return None, "Channel unavailable"
        raise


def main():
    print("Authenticating...")
    creds = authenticate()
    youtube = build("youtube", "v3", credentials=creds)

    print("Fetching subscriptions...")
    subs = get_all_subscriptions(youtube)
    print(f"Found {len(subs)} subscriptions. Checking last upload dates...\n")

    all_channels = []
    inactive_count = 0
    for i, sub in enumerate(subs, 1):
        last_upload_dt, last_upload_str = get_last_upload_date(youtube, sub["channel_id"])
        is_inactive = last_upload_dt is None or last_upload_dt < CUTOFF_DATE
        status = "INACTIVE" if is_inactive else "ACTIVE"
        if is_inactive:
            inactive_count += 1

        print(f"  [{i}/{len(subs)}] {sub['channel_title']:<40} Last upload: {last_upload_str:<14} [{status}]")

        all_channels.append({
            "channel_title": sub["channel_title"],
            "channel_url": f"https://www.youtube.com/channel/{sub['channel_id']}",
            "last_upload": last_upload_str,
            "status": status,
            "subscription_id": sub["subscription_id"],
            "channel_id": sub["channel_id"],
        })

    print(f"\n{'='*60}")
    print(f"Total subscriptions: {len(subs)}")
    print(f"Inactive (no upload since {CUTOFF_DATE.date()}): {inactive_count}")
    print(f"Active: {len(subs) - inactive_count}")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "channel_title", "channel_url", "last_upload", "status",
            "subscription_id", "channel_id",
        ])
        writer.writeheader()
        writer.writerows(all_channels)
    print(f"\nAll channels saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
