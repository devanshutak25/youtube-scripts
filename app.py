"""
YouTube Subscription Cleanup — Web UI
======================================
Flask app wrapping yt_cleanup.py with a browser-based interface.

Run:
    pip install flask
    python app.py

Then open http://localhost:5000 in your browser.
"""

import csv
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request

from yt_cleanup import (
    CLIENT_SECRET_FILE,
    COST_DELETE,
    COST_LIST,
    HISTORY_FILE,
    HISTORY_FIELDS,
    LOG_FILE,
    OUTPUT_FIELDS,
    TOKEN_READ_FILE,
    TOKEN_WRITE_FILE,
    QuotaTracker,
    authenticate,
    batch_get_channel_info,
    get_all_subscriptions,
    get_last_upload_date,
    setup_logging,
)

app = Flask(__name__)

# ── Global state (single-user local app) ────────────────────────────────────

state = {
    "scan_running": False,
    "unsub_running": False,
    "results": [],           # latest scan results
    "results_file": None,    # path to last saved results file
}

progress_queue = queue.Queue()   # shared queue for SSE streaming


# ── Helpers ─────────────────────────────────────────────────────────────────

def _check_setup():
    return os.path.exists(CLIENT_SECRET_FILE)


def _check_auth(token_file):
    return os.path.exists(token_file)


def _load_results_from_disk(fmt="csv"):
    """Load previously saved results file."""
    filepath = f"inactive_channels.{fmt}"
    if not os.path.exists(filepath):
        return []
    if fmt == "json":
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        with open(filepath, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))


def _load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _save_results(channels, fmt="csv"):
    filepath = f"inactive_channels.{fmt}"
    if fmt == "json":
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(channels, f, indent=2, ensure_ascii=False)
    else:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(channels)
    return filepath


def _archive_channel(channel):
    file_exists = os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not file_exists:
            writer.writeheader()
        row = {k: channel.get(k, "") for k in HISTORY_FIELDS}
        row["unsubscribed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow(row)


def _remove_from_results(removed_ids):
    """Remove unsubscribed channels from in-memory results and disk."""
    state["results"] = [r for r in state["results"] if r.get("subscription_id") not in removed_ids]
    _save_results(state["results"], "csv")


# ── SSE helper ──────────────────────────────────────────────────────────────

def _sse_stream():
    """Generator that reads from progress_queue and yields SSE events."""
    while True:
        try:
            event = progress_queue.get(timeout=30)
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("complete", "error"):
                break
        except queue.Empty:
            yield ": keepalive\n\n"


# ── Background workers ─────────────────────────────────────────────────────

def _scan_worker(years, fmt):
    logger = setup_logging(LOG_FILE)
    quota = QuotaTracker()

    try:
        now = datetime.now(timezone.utc)
        cutoff_date = datetime(now.year - years, now.month, now.day, tzinfo=timezone.utc)

        progress_queue.put({
            "type": "status",
            "message": f"Cutoff: {cutoff_date.date()} ({years} year{'s' if years != 1 else ''} ago)",
        })

        # Authenticate
        progress_queue.put({"type": "status", "message": "Authenticating..."})
        creds = authenticate(
            ["https://www.googleapis.com/auth/youtube.readonly"],
            TOKEN_READ_FILE,
        )
        from googleapiclient.discovery import build as yt_build
        youtube = yt_build("youtube", "v3", credentials=creds)

        # Fetch subscriptions
        progress_queue.put({"type": "status", "message": "Fetching subscriptions..."})
        subs = get_all_subscriptions(youtube, quota)
        progress_queue.put({
            "type": "subscriptions",
            "total": len(subs),
            "message": f"Found {len(subs)} subscriptions",
        })

        # Load existing for resume
        existing = {}
        filepath = f"inactive_channels.{fmt}"
        if os.path.exists(filepath):
            if fmt == "json":
                with open(filepath, "r", encoding="utf-8") as f:
                    for row in json.load(f):
                        existing[row["channel_id"]] = row
            else:
                with open(filepath, "r", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        existing[row["channel_id"]] = row

        new_subs = [s for s in subs if s["channel_id"] not in existing]

        if existing:
            progress_queue.put({
                "type": "status",
                "message": f"Resuming: {len(existing)} cached, {len(new_subs)} to scan",
            })

        if new_subs:
            # Batch fetch channel info
            progress_queue.put({"type": "status", "message": "Fetching channel info..."})
            channel_ids = [s["channel_id"] for s in new_subs]
            channel_info = batch_get_channel_info(youtube, channel_ids, quota)

            # Check each channel
            for idx, sub in enumerate(new_subs):
                cid = sub["channel_id"]
                info = channel_info.get(cid)

                if info is None:
                    last_dt, last_str = None, "Channel not found"
                    sub_count, view_count = "N/A", "N/A"
                else:
                    last_dt, last_str = get_last_upload_date(youtube, info["uploads_playlist"], quota)
                    sub_count = info["subscriber_count"]
                    view_count = info["view_count"]

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

                progress_queue.put({
                    "type": "channel",
                    "index": idx + 1,
                    "total": len(new_subs),
                    "name": sub["channel_title"],
                    "last_upload": last_str,
                    "status": status,
                    "subscriber_count": str(sub_count),
                })

                if not quota.can_afford(COST_LIST):
                    progress_queue.put({
                        "type": "status",
                        "message": f"Quota warning: {quota.summary()}. Progress saved.",
                    })
                    break

        # Build ordered results
        sub_order = {s["channel_id"]: i for i, s in enumerate(subs)}
        all_channels = sorted(existing.values(), key=lambda r: sub_order.get(r["channel_id"], 999999))

        # Save
        filepath = _save_results(all_channels, fmt)
        state["results"] = all_channels
        state["results_file"] = filepath

        inactive_count = sum(1 for c in all_channels if c["status"] == "INACTIVE")

        progress_queue.put({
            "type": "complete",
            "total": len(all_channels),
            "inactive": inactive_count,
            "active": len(all_channels) - inactive_count,
            "quota_used": quota.used,
            "file": filepath,
        })
        logger.info("Web scan complete: %d total, %d inactive", len(all_channels), inactive_count)

    except Exception as e:
        progress_queue.put({"type": "error", "message": str(e)})
        logger.error("Scan error: %s", e, exc_info=True)
    finally:
        state["scan_running"] = False


def _unsub_worker(subscription_ids, dry_run):
    logger = setup_logging(LOG_FILE)
    quota = QuotaTracker()

    # Build lookup from current results
    channels_by_id = {r["subscription_id"]: r for r in state["results"]}
    targets = [channels_by_id[sid] for sid in subscription_ids if sid in channels_by_id]

    if not targets:
        progress_queue.put({"type": "error", "message": "No matching channels found."})
        state["unsub_running"] = False
        return

    if dry_run:
        estimated_cost = len(targets) * COST_DELETE
        progress_queue.put({
            "type": "complete",
            "dry_run": True,
            "total": len(targets),
            "estimated_quota": estimated_cost,
            "channels": [t["channel_title"] for t in targets],
        })
        state["unsub_running"] = False
        return

    try:
        progress_queue.put({"type": "status", "message": "Authenticating (write access)..."})
        creds = authenticate(
            ["https://www.googleapis.com/auth/youtube"],
            TOKEN_WRITE_FILE,
        )
        from googleapiclient.discovery import build as yt_build
        from googleapiclient.errors import HttpError
        youtube = yt_build("youtube", "v3", credentials=creds)

        success = 0
        failed = 0
        removed_ids = set()

        for idx, ch in enumerate(targets):
            sub_id = ch["subscription_id"]
            title = ch["channel_title"]

            if not quota.can_afford(COST_DELETE):
                progress_queue.put({
                    "type": "status",
                    "message": f"Quota limit reached. {quota.summary()}",
                })
                break

            try:
                youtube.subscriptions().delete(id=sub_id).execute()
                quota.consume(COST_DELETE)
                success += 1
                removed_ids.add(sub_id)
                _archive_channel(ch)

                progress_queue.put({
                    "type": "channel",
                    "index": idx + 1,
                    "total": len(targets),
                    "name": title,
                    "result": "success",
                })
                time.sleep(0.2)

            except HttpError as e:
                if e.resp.status == 403 and "quotaExceeded" in str(e):
                    progress_queue.put({
                        "type": "status",
                        "message": f"Quota exceeded after {success} unsubscribes. Re-run tomorrow.",
                    })
                    break
                elif e.resp.status == 404:
                    success += 1
                    removed_ids.add(sub_id)
                    _archive_channel(ch)
                    progress_queue.put({
                        "type": "channel",
                        "index": idx + 1,
                        "total": len(targets),
                        "name": title,
                        "result": "already_removed",
                    })
                else:
                    failed += 1
                    progress_queue.put({
                        "type": "channel",
                        "index": idx + 1,
                        "total": len(targets),
                        "name": title,
                        "result": "failed",
                        "error": str(e),
                    })

        _remove_from_results(removed_ids)

        progress_queue.put({
            "type": "complete",
            "success": success,
            "failed": failed,
            "remaining": len(targets) - success - failed,
            "quota_used": quota.used,
        })
        logger.info("Web unsub complete: %d success, %d failed", success, failed)

    except Exception as e:
        progress_queue.put({"type": "error", "message": str(e)})
        logger.error("Unsub error: %s", e, exc_info=True)
    finally:
        state["unsub_running"] = False


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    # Load results from disk if not in memory
    if not state["results"]:
        state["results"] = _load_results_from_disk("csv")

    inactive = sum(1 for r in state["results"] if r.get("status") == "INACTIVE")
    return jsonify({
        "setup_ok": _check_setup(),
        "read_auth": _check_auth(TOKEN_READ_FILE),
        "write_auth": _check_auth(TOKEN_WRITE_FILE),
        "scan_running": state["scan_running"],
        "unsub_running": state["unsub_running"],
        "has_results": len(state["results"]) > 0,
        "total_channels": len(state["results"]),
        "inactive_channels": inactive,
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if state["scan_running"] or state["unsub_running"]:
        return jsonify({"error": "An operation is already running."}), 409

    data = request.get_json(force=True)
    years = int(data.get("years", 2))
    fmt = data.get("format", "csv")

    if years < 1:
        return jsonify({"error": "Years must be at least 1."}), 400

    # Drain any leftover events from a previous run
    while not progress_queue.empty():
        try:
            progress_queue.get_nowait()
        except queue.Empty:
            break

    state["scan_running"] = True
    thread = threading.Thread(target=_scan_worker, args=(years, fmt), daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": f"Scan started ({years} year cutoff)"})


@app.route("/api/scan/stream")
def api_scan_stream():
    return Response(_sse_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/results")
def api_results():
    if not state["results"]:
        state["results"] = _load_results_from_disk("csv")
    return jsonify(state["results"])


@app.route("/api/unsub", methods=["POST"])
def api_unsub():
    if state["scan_running"] or state["unsub_running"]:
        return jsonify({"error": "An operation is already running."}), 409

    data = request.get_json(force=True)
    subscription_ids = data.get("subscription_ids", [])
    dry_run = data.get("dry_run", False)

    if not subscription_ids:
        return jsonify({"error": "No channels selected."}), 400

    while not progress_queue.empty():
        try:
            progress_queue.get_nowait()
        except queue.Empty:
            break

    state["unsub_running"] = True
    thread = threading.Thread(target=_unsub_worker, args=(subscription_ids, dry_run), daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": f"{'Dry run' if dry_run else 'Unsubscribe'} started ({len(subscription_ids)} channels)"})


@app.route("/api/unsub/stream")
def api_unsub_stream():
    return Response(_sse_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/history")
def api_history():
    return jsonify(_load_history())


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Load any existing results into memory on startup
    state["results"] = _load_results_from_disk("csv")
    print("\n  YouTube Subscription Cleanup")
    print("  Open http://localhost:5000 in your browser\n")
    app.run(debug=False, port=5000, threaded=True)
