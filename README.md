# YouTube Subscription Cleanup Tool

Find and remove YouTube channels you're subscribed to that haven't uploaded in years. Available as a **web UI** (recommended) or **CLI**.

## Prerequisites

- Python 3.8+
- A Google Cloud project with **YouTube Data API v3** enabled
- OAuth 2.0 credentials (Desktop app type)

## Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or use an existing one)
3. Enable **YouTube Data API v3** (APIs & Services → Library)
4. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
5. Download the credentials JSON and save it as `client_secret.json` in this directory
6. Run the app:

```bash
python app.py
```

That's it — all Python dependencies (Flask, Google API client, etc.) are installed automatically on first run.

## Web UI (Recommended)

The easiest way to use this tool — no command-line knowledge needed. Just run `python app.py` and open **http://localhost:5000** in your browser.

The UI has three sections:

| Tab | What it does |
|---|---|
| **Scan** | Configure the inactivity threshold and scan all your subscriptions with live progress |
| **Results** | Browse, search, filter, and sort your channels. Select the ones you want to unsubscribe from. Supports dry-run to preview before committing. |
| **History** | View all channels you've previously unsubscribed from (archived, never deleted) |

The web UI includes all the features of the CLI: batched API calls, resume support, quota tracking, and channel statistics.

## CLI Usage

Dependencies are also auto-installed when using the CLI via `app.py`. If using `yt_cleanup.py` directly, install manually: `pip install google-api-python-client google-auth-oauthlib tqdm`

### Step 1: Scan subscriptions

```bash
python yt_cleanup.py scan
```

Prompts for an inactivity threshold (default: 2 years), then checks every subscription's last upload date. Results are saved to `inactive_channels.csv`.

**Options:**

| Flag | Description |
|---|---|
| `--years N` | Set inactivity threshold directly (skip prompt) |
| `--format csv\|json` | Output format (default: csv) |
| `--output NAME` | Output file base name without extension (default: `inactive_channels`) |

**Examples:**

```bash
python yt_cleanup.py scan --years 3
python yt_cleanup.py scan --years 1 --format json --output results
```

**Resume support:** If the script is interrupted (quota limit, crash, etc.), re-run the same command — it picks up where it left off automatically.

### Step 2: Review

Open `inactive_channels.csv` and review the list. The CSV includes subscriber count and view count to help you decide. Remove any rows you want to **keep** subscribed to. Only rows with `status = INACTIVE` are processed by the unsubscribe step.

### Step 3: Unsubscribe

```bash
python yt_cleanup.py unsub
```

Shows the list of inactive channels and asks for confirmation before unsubscribing.

**Options:**

| Flag | Description |
|---|---|
| `--input FILE` | Input file from scan (default: `inactive_channels.csv`) |
| `--dry-run` | Show what would happen without actually unsubscribing |
| `--interactive` | Review channels one by one and choose which to unsubscribe |

**Examples:**

```bash
python yt_cleanup.py unsub --dry-run
python yt_cleanup.py unsub --interactive
python yt_cleanup.py unsub --input results.json
```

Unsubscribed channels are archived to `unsubscribed_history.csv` (never deleted) and removed from the source file so re-runs skip them.

## API Quota

YouTube Data API has a daily quota of **10,000 units**. The tool tracks quota usage and warns before hitting the limit.

| Operation | Cost | Notes |
|---|---|---|
| `subscriptions.list` | 1 unit/call | Paginated, 50 per call |
| `channels.list` | 1 unit/call | **Batched** — up to 50 channels per call |
| `playlistItems.list` | 1 unit/call | 1 per channel (last upload check) |
| `subscriptions.delete` | 50 units/call | ~200 unsubscribes per day max |

If quota is exceeded, re-run the next day — both `scan` and `unsub` resume where they left off.

## Files

| File | Description |
|---|---|
| `app.py` | Web UI (Flask server) |
| `yt_cleanup.py` | CLI tool |
| `client_secret.json` | Your OAuth credentials (do not commit) |
| `token.json` | Cached read-only auth token |
| `token_write.json` | Cached write auth token |
| `inactive_channels.csv` | Generated list of all subscriptions with status |
| `unsubscribed_history.csv` | Archive of all unsubscribed channels with timestamps |
| `yt_cleanup.log` | Debug log for troubleshooting |

## Legacy Scripts

The original two-script workflow (`youtube_inactive_subs.py` and `youtube_unsub_inactive.py`) still works but `yt_cleanup.py` is the recommended replacement with batched API calls, resume support, quota tracking, and more.
