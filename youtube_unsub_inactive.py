"""
YouTube Inactive Channel Unsubscriber
======================================
Reads inactive_channels.csv (from youtube_inactive_subs.py) and unsubscribes
from all channels marked INACTIVE.

NOTE: This script requires the full `youtube` OAuth scope (not readonly).
      It uses a separate token file (token_write.json) so it won't conflict
      with the reader script. First run will prompt for OAuth consent again.

Quota: subscriptions.delete = 50 units/call. Daily limit = 10,000 units.
       Max ~200 unsubscribes per day. If you hit the limit, re-run tomorrow.
"""

import csv
import os
import sys
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/youtube"]
CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token_write.json"
CSV_FILE = "inactive_channels.csv"


def authenticate():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_FILE):
                print(f"ERROR: '{CLIENT_SECRET_FILE}' not found.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def load_inactive_channels():
    if not os.path.exists(CSV_FILE):
        print(f"ERROR: '{CSV_FILE}' not found. Run youtube_inactive_subs.py first.")
        sys.exit(1)

    inactive = []
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("status", "").strip().upper() == "INACTIVE":
                inactive.append(row)
    return inactive


def main():
    inactive = load_inactive_channels()

    if not inactive:
        print("No inactive channels found in CSV.")
        return

    print(f"Found {len(inactive)} inactive channels to unsubscribe from:\n")
    for i, ch in enumerate(inactive, 1):
        print(f"  {i:>4}. {ch['channel_title']:<40} Last upload: {ch['last_upload']}")

    print(f"\n{'='*60}")
    confirm = input(f"Unsubscribe from all {len(inactive)} channels? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    print("\nAuthenticating (write access)...")
    creds = authenticate()
    youtube = build("youtube", "v3", credentials=creds)

    success = 0
    failed = 0
    quota_hit = False

    for i, ch in enumerate(inactive, 1):
        sub_id = ch["subscription_id"]
        title = ch["channel_title"]

        try:
            youtube.subscriptions().delete(id=sub_id).execute()
            print(f"  [{i}/{len(inactive)}] Unsubscribed: {title}")
            success += 1
            time.sleep(0.2)  # gentle rate limiting

        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e):
                print(f"\n  QUOTA EXCEEDED at {success} unsubscribes.")
                print("  Re-run this script tomorrow to continue.")
                quota_hit = True
                break
            elif e.resp.status == 404:
                print(f"  [{i}/{len(inactive)}] Already unsubscribed: {title}")
                success += 1  # count as resolved
            else:
                print(f"  [{i}/{len(inactive)}] FAILED: {title} — {e}")
                failed += 1

    print(f"\n{'='*60}")
    print(f"Unsubscribed: {success}")
    print(f"Failed:       {failed}")
    if quota_hit:
        remaining = len(inactive) - success - failed
        print(f"Remaining:    {remaining} (re-run tomorrow)")


if __name__ == "__main__":
    main()
