"""
YouTube Subscription Cleanup Tool
==================================
A single CLI tool to find inactive YouTube subscriptions and bulk-unsubscribe.

Subcommands:
  scan   — Scan all subscriptions, flag inactive channels, save to CSV/JSON
  unsub  — Unsubscribe from inactive channels (with dry-run, selective, and archive)

Run `python yt_cleanup.py <command> --help` for per-command options.
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# ── Defaults ────────────────────────────────────────────────────────────────

CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_READ_FILE = "token.json"
TOKEN_WRITE_FILE = "token_write.json"
DEFAULT_OUTPUT = "inactive_channels"
HISTORY_FILE = "unsubscribed_history.csv"
LOG_FILE = "yt_cleanup.log"
DAILY_QUOTA = 10_000

# API unit costs
COST_LIST = 1        # subscriptions.list, channels.list, playlistItems.list
COST_DELETE = 50     # subscriptions.delete

HISTORY_FIELDS = [
    "channel_title", "channel_url", "last_upload", "subscriber_count",
    "view_count", "subscription_id", "channel_id", "unsubscribed_at",
]

OUTPUT_FIELDS = [
    "channel_title", "channel_url", "last_upload", "status",
    "subscriber_count", "view_count", "subscription_id", "channel_id",
]


# ── Logging ─────────────────────────────────────────────────────────────────

def setup_logging(log_file):
    logger = logging.getLogger("yt_cleanup")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
    logger.addHandler(fh)
    return logger


# ── Quota tracker ───────────────────────────────────────────────────────────

class QuotaTracker:
    def __init__(self):
        self.used = 0

    def consume(self, units):
        self.used += units

    def remaining(self):
        return DAILY_QUOTA - self.used

    def can_afford(self, units):
        return self.remaining() >= units

    def summary(self):
        return f"Quota used: {self.used:,} / {DAILY_QUOTA:,} ({self.remaining():,} remaining)"


# ── Auth ────────────────────────────────────────────────────────────────────

def authenticate(scopes, token_file):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_FILE):
                print(f"ERROR: '{CLIENT_SECRET_FILE}' not found in current directory.")
                print("Download it from Google Cloud Console -> APIs & Services -> Credentials")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return creds


# ── Progress helper ─────────────────────────────────────────────────────────

def progress_bar(iterable, total, desc="Processing"):
    """Use tqdm if available, otherwise a simple print-based fallback."""
    if tqdm is not None:
        return tqdm(iterable, total=total, desc=desc, unit="ch")

    # Fallback: yield items and print progress every 10 items or at end
    def _fallback():
        for i, item in enumerate(iterable, 1):
            yield item
            if i % 10 == 0 or i == total:
                print(f"  {desc}: {i}/{total}")
    return _fallback()


# ── Scan command ────────────────────────────────────────────────────────────

def get_all_subscriptions(youtube, quota):
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
        quota.consume(COST_LIST)
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


def batch_get_channel_info(youtube, channel_ids, quota):
    """Fetch upload playlist IDs and statistics for up to 50 channels at once."""
    info = {}
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i:i + 50]
        resp = youtube.channels().list(
            part="contentDetails,statistics",
            id=",".join(batch),
        ).execute()
        quota.consume(COST_LIST)
        for item in resp.get("items", []):
            cid = item["id"]
            info[cid] = {
                "uploads_playlist": item["contentDetails"]["relatedPlaylists"]["uploads"],
                "subscriber_count": item["statistics"].get("subscriberCount", "N/A"),
                "view_count": item["statistics"].get("viewCount", "N/A"),
            }
    return info


def get_last_upload_date(youtube, playlist_id, quota):
    """Get the most recent upload date from an uploads playlist."""
    try:
        resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=1,
        ).execute()
        quota.consume(COST_LIST)
        items = resp.get("items", [])
        if not items:
            return None, "No uploads"
        published = items[0]["contentDetails"]["videoPublishedAt"]
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        return dt, dt.strftime("%Y-%m-%d")
    except HttpError as e:
        if e.resp.status == 404:
            return None, "Channel unavailable"
        raise


def load_existing_results(output_base, fmt):
    """Load already-scanned channels from a previous run for resume support."""
    existing = {}
    filepath = f"{output_base}.{fmt}"
    if not os.path.exists(filepath):
        return existing

    if fmt == "csv":
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing[row["channel_id"]] = row
    else:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            for row in data:
                existing[row["channel_id"]] = row
    return existing


def save_results(all_channels, output_base, fmt):
    filepath = f"{output_base}.{fmt}"
    if fmt == "json":
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(all_channels, f, indent=2, ensure_ascii=False)
    else:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(all_channels)
    return filepath


def cmd_scan(args):
    logger = setup_logging(LOG_FILE)
    quota = QuotaTracker()

    # Determine cutoff
    if args.years is None:
        try:
            raw = input("How many years of inactivity to flag a channel? (default: 2): ").strip()
            years = int(raw) if raw else 2
            if years < 1:
                print("Please enter a positive number.")
                sys.exit(1)
        except ValueError:
            print("Please enter a valid number.")
            sys.exit(1)
    else:
        years = args.years

    now = datetime.now(timezone.utc)
    cutoff_date = datetime(now.year - years, now.month, now.day, tzinfo=timezone.utc)
    print(f"\nCutoff date: {cutoff_date.date()} ({years} year{'s' if years != 1 else ''} ago)\n")
    logger.info("Scan started — cutoff: %s (%d years)", cutoff_date.date(), years)

    # Resume support: load existing results
    fmt = args.format
    output_base = args.output
    existing = load_existing_results(output_base, fmt)
    if existing:
        print(f"Resuming: {len(existing)} channels already scanned from previous run.\n")
        logger.info("Resuming with %d previously scanned channels", len(existing))

    # Authenticate and fetch subscriptions
    print("Authenticating...")
    creds = authenticate(
        ["https://www.googleapis.com/auth/youtube.readonly"],
        TOKEN_READ_FILE,
    )
    youtube = build("youtube", "v3", credentials=creds)

    print("Fetching subscriptions...")
    subs = get_all_subscriptions(youtube, quota)
    print(f"Found {len(subs)} subscriptions.\n")
    logger.info("Found %d subscriptions", len(subs))

    # Separate already-scanned vs new
    new_subs = [s for s in subs if s["channel_id"] not in existing]
    print(f"Channels to scan: {len(new_subs)} new, {len(existing)} cached.\n")

    if new_subs:
        # Batch-fetch channel info (uploads playlist + stats)
        print("Fetching channel info in batches...")
        new_channel_ids = [s["channel_id"] for s in new_subs]
        channel_info = batch_get_channel_info(youtube, new_channel_ids, quota)

        # Check last upload for each new channel
        print("Checking last upload dates...\n")
        for sub in progress_bar(new_subs, total=len(new_subs), desc="Scanning"):
            cid = sub["channel_id"]
            info = channel_info.get(cid)

            if info is None:
                last_dt, last_str = None, "Channel not found"
                sub_count, view_count = "N/A", "N/A"
            else:
                last_dt, last_str = get_last_upload_date(youtube, info["uploads_playlist"], quota)
                sub_count = info["subscriber_count"]
                view_count = info["view_count"]

                if not quota.can_afford(COST_LIST):
                    print(f"\nWARNING: Approaching quota limit. {quota.summary()}")
                    print("Progress saved — re-run to continue.\n")
                    logger.warning("Quota warning during scan: %s", quota.summary())
                    break

            is_inactive = last_dt is None or last_dt < cutoff_date
            status = "INACTIVE" if is_inactive else "ACTIVE"

            existing[cid] = {
                "channel_title": sub["channel_title"],
                "channel_url": f"https://www.youtube.com/channel/{cid}",
                "last_upload": last_str,
                "status": status,
                "subscriber_count": str(sub_count),
                "view_count": str(view_count),
                "subscription_id": sub["subscription_id"],
                "channel_id": cid,
            }

            logger.debug("%s — %s — %s", sub["channel_title"], last_str, status)

    # Build final list (preserve subscription order)
    sub_order = {s["channel_id"]: i for i, s in enumerate(subs)}
    all_channels = sorted(existing.values(), key=lambda r: sub_order.get(r["channel_id"], 999999))

    inactive_count = sum(1 for c in all_channels if c["status"] == "INACTIVE")
    active_count = len(all_channels) - inactive_count

    print(f"\n{'=' * 60}")
    print(f"Total subscriptions: {len(all_channels)}")
    print(f"Inactive (no upload since {cutoff_date.date()}): {inactive_count}")
    print(f"Active: {active_count}")
    print(f"{quota.summary()}")

    filepath = save_results(all_channels, output_base, fmt)
    print(f"\nResults saved to: {filepath}")
    logger.info("Scan complete — %d inactive, %d active. Saved to %s", inactive_count, active_count, filepath)
    logger.info(quota.summary())


# ── Unsub command ───────────────────────────────────────────────────────────

def load_inactive_channels(input_file):
    if not os.path.exists(input_file):
        print(f"ERROR: '{input_file}' not found. Run `scan` first.")
        sys.exit(1)

    if input_file.endswith(".json"):
        with open(input_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [r for r in data if r.get("status", "").strip().upper() == "INACTIVE"]
    else:
        inactive = []
        with open(input_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status", "").strip().upper() == "INACTIVE":
                    inactive.append(row)
        return inactive


def archive_channel(channel, history_file):
    """Append an unsubscribed channel to the history file."""
    file_exists = os.path.exists(history_file)
    with open(history_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not file_exists:
            writer.writeheader()
        row = {k: channel.get(k, "") for k in HISTORY_FIELDS}
        row["unsubscribed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow(row)


def remove_from_source(input_file, removed_ids):
    """Remove unsubscribed channels from the source CSV/JSON."""
    if not removed_ids:
        return

    if input_file.endswith(".json"):
        with open(input_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = [r for r in data if r.get("subscription_id") not in removed_ids]
        with open(input_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    else:
        rows = []
        with open(input_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                if row.get("subscription_id") not in removed_ids:
                    rows.append(row)
        with open(input_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def cmd_unsub(args):
    logger = setup_logging(LOG_FILE)
    quota = QuotaTracker()

    input_file = args.input
    inactive = load_inactive_channels(input_file)

    if not inactive:
        print("No inactive channels found.")
        return

    # ── Selective mode ──────────────────────────────────────────────────
    if args.interactive:
        selected = []
        print(f"\nReview {len(inactive)} inactive channels one by one:\n")
        for i, ch in enumerate(inactive, 1):
            subs = ch.get("subscriber_count", "N/A")
            print(f"  [{i}/{len(inactive)}] {ch['channel_title']}")
            print(f"           Last upload: {ch['last_upload']}  |  Subscribers: {subs}")
            choice = input("           Unsubscribe? (y/n/q to quit): ").strip().lower()
            if choice == "q":
                break
            if choice == "y":
                selected.append(ch)
        inactive = selected
        if not inactive:
            print("\nNo channels selected. Aborted.")
            return
        print(f"\nSelected {len(inactive)} channels to unsubscribe.\n")
    else:
        print(f"\n{len(inactive)} inactive channels to unsubscribe from:\n")
        for i, ch in enumerate(inactive, 1):
            subs = ch.get("subscriber_count", "N/A")
            print(f"  {i:>4}. {ch['channel_title']:<40} Last upload: {ch['last_upload']}  Subs: {subs}")

    # ── Dry run ─────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN: Would unsubscribe from {len(inactive)} channels.")
        estimated_cost = len(inactive) * COST_DELETE
        print(f"Estimated quota cost: {estimated_cost:,} units")
        logger.info("Dry run — %d channels, estimated cost %d units", len(inactive), estimated_cost)
        return

    # ── Confirmation ────────────────────────────────────────────────────
    if not args.interactive:
        print(f"\n{'=' * 60}")
        confirm = input(f"Unsubscribe from all {len(inactive)} channels? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return

    # ── Authenticate and unsubscribe ────────────────────────────────────
    print("\nAuthenticating (write access)...")
    creds = authenticate(
        ["https://www.googleapis.com/auth/youtube"],
        TOKEN_WRITE_FILE,
    )
    youtube = build("youtube", "v3", credentials=creds)

    success = 0
    failed = 0
    removed_ids = set()

    for i, ch in enumerate(inactive, 1):
        sub_id = ch["subscription_id"]
        title = ch["channel_title"]

        if not quota.can_afford(COST_DELETE):
            print(f"\nQuota limit approaching. {quota.summary()}")
            print("Re-run tomorrow to continue.")
            logger.warning("Quota limit reached at %d unsubs: %s", success, quota.summary())
            break

        try:
            youtube.subscriptions().delete(id=sub_id).execute()
            quota.consume(COST_DELETE)
            print(f"  [{i}/{len(inactive)}] Unsubscribed: {title}")
            logger.info("Unsubscribed: %s (%s)", title, sub_id)
            success += 1
            removed_ids.add(sub_id)
            archive_channel(ch, HISTORY_FILE)
            time.sleep(0.2)

        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e):
                print(f"\n  QUOTA EXCEEDED after {success} unsubscribes.")
                print("  Re-run tomorrow to continue.")
                logger.warning("Quota exceeded at %d unsubs", success)
                break
            elif e.resp.status == 404:
                print(f"  [{i}/{len(inactive)}] Already unsubscribed: {title}")
                logger.info("Already unsubscribed: %s", title)
                success += 1
                removed_ids.add(sub_id)
                archive_channel(ch, HISTORY_FILE)
            else:
                print(f"  [{i}/{len(inactive)}] FAILED: {title} — {e}")
                logger.error("Failed: %s — %s", title, e)
                failed += 1

    # Remove processed channels from source file
    remove_from_source(input_file, removed_ids)

    print(f"\n{'=' * 60}")
    print(f"Unsubscribed: {success}")
    print(f"Failed:       {failed}")
    remaining = len(inactive) - success - failed
    if remaining > 0:
        print(f"Remaining:    {remaining} (re-run tomorrow)")
    if removed_ids:
        print(f"Archived to:  {HISTORY_FILE}")
    print(quota.summary())
    logger.info("Unsub complete — success: %d, failed: %d, remaining: %d", success, failed, remaining)
    logger.info(quota.summary())


# ── CLI ─────────────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog="yt_cleanup",
        description="YouTube Subscription Cleanup Tool — find and remove inactive subscriptions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    scan_p = sub.add_parser("scan", help="Scan subscriptions and flag inactive channels")
    scan_p.add_argument("--years", type=int, default=None,
                        help="Years of inactivity to flag (interactive prompt if omitted)")
    scan_p.add_argument("--output", default=DEFAULT_OUTPUT,
                        help=f"Output file base name without extension (default: {DEFAULT_OUTPUT})")
    scan_p.add_argument("--format", choices=["csv", "json"], default="csv",
                        help="Output format (default: csv)")

    # unsub
    unsub_p = sub.add_parser("unsub", help="Unsubscribe from inactive channels")
    unsub_p.add_argument("--input", default=f"{DEFAULT_OUTPUT}.csv",
                         help=f"Input file from scan (default: {DEFAULT_OUTPUT}.csv)")
    unsub_p.add_argument("--dry-run", action="store_true",
                         help="Show what would happen without actually unsubscribing")
    unsub_p.add_argument("--interactive", action="store_true",
                         help="Review channels one by one and choose which to unsubscribe")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "unsub":
        cmd_unsub(args)


if __name__ == "__main__":
    main()
