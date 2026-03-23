# YouTube Inactive Subscriptions Cleaner

Two scripts to find and remove YouTube channels you're subscribed to that haven't uploaded in years.

| Script | Purpose |
|---|---|
| `youtube_inactive_subs.py` | Scans all your subscriptions, prompts you for an inactivity threshold (in years), flags inactive channels, saves results to CSV |
| `youtube_unsub_inactive.py` | Reads that CSV and bulk-unsubscribes from all inactive channels |

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
6. Install dependencies:

```bash
pip install google-api-python-client google-auth-oauthlib
```

## Usage

### Step 1: Find inactive channels

```bash
python youtube_inactive_subs.py
```

- Prompts you for how many years of inactivity to use as the cutoff (default: 2)
- Opens a browser for OAuth consent on first run (read-only access)
- Checks every subscribed channel's last upload date
- Outputs `inactive_channels.csv` with columns: `channel_title`, `channel_url`, `last_upload`, `status`, `subscription_id`, `channel_id`
- Token is cached in `token.json` for future runs

### Step 2: Review the CSV

Open `inactive_channels.csv` and review the list. Remove any rows you want to **keep** subscribed to. The unsubscribe script only processes rows with `status = INACTIVE`.

### Step 3: Unsubscribe

```bash
python youtube_unsub_inactive.py
```

- Shows the list of channels to unsubscribe from and asks for confirmation (`yes`/`no`)
- Uses a separate token (`token_write.json`) with write access — will prompt for OAuth consent on first run
- Includes rate limiting (0.2s between calls)

## API Quota

YouTube Data API has a daily quota of **10,000 units**:

| Operation | Cost |
|---|---|
| `subscriptions.list` | 1 unit/call |
| `channels.list` | 1 unit/call |
| `playlistItems.list` | 1 unit/call |
| `subscriptions.delete` | 50 units/call |

The unsubscribe script can handle roughly **200 unsubscribes per day**. If quota is exceeded, re-run the script the next day — it will resume where it left off.

## Files

| File | Description |
|---|---|
| `client_secret.json` | Your OAuth credentials (do not commit) |
| `token.json` | Cached read-only auth token |
| `token_write.json` | Cached write auth token |
| `inactive_channels.csv` | Generated list of all subscriptions with status |
