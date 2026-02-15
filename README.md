# thps.run Archiver
Specialized bot (within a Docker container) that automatically archives YouTube videos once they are approved in the thps.run API. It will continually poll thps.run for submissions, download them via the `yt-dlp` library, upload them to a BackBlaze bucket, and update the submission with the archived URL.

As such, this project is mainly meant for thps.run support, but it is open-source if you need ideas of your own!

## Requirements
- Docker and Docker Compose
- A Google/YouTube account for cookie authentication
- BackBlaze B2 bucket
- A Django API serving run data (see [API](#api-expectations) below)

## Setup

1. **Clone and configure environment**

   `cp .env.example .env`

   * Fill in `.env` with your credentials:
     - `API_BASE_URL` / `API_KEY` - your Django API endpoint and auth key
     - `B2_APPLICATION_KEY_ID` / `B2_APPLICATION_KEY` / `B2_BUCKET_NAME` - BackBlaze B2 credentials
     - `SENTRY_DSN` - optional, leave empty to disable error reporting

2. **Export YouTube cookies**

    The bot needs cookies from a logged-in YouTube session to download videos reliably. Use a browser extension like [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) to export cookies in Netscape format, then place the file at: `data/cookies.txt`.

   > [!TIP]
   > Use a burner Google account for this. YouTube may flag the account for automated activity, which is very bad if you are using your real account.

3. **Start the bot**

    `docker compose up --build -d`

4. **View Bot Logs**

    `docker compose logs -f archiver`

5. **Execute Commands Within Container**

    `docker exec -it youtube-archiver /bin/bash`

## How It Works

The bot runs a continuous polling loop:

1. Fetches pending runs from the thps.run API (`GET /runs/?needs_archive=true`).
2. Skips any already processed or queued locally (tracked in a local SQLite database).
3. Downloads each video via yt-dlp (configured in `yt-dlp.conf`).
4. Uploads the file to BackBlaze B2 under `{run_id}.mp4`.
5. PATCHes the thps.run API with the archive URL (if it has the right powers and if it is enabled).
    *   DISABLED BEFORE PROD.
6. Waits 30–90 seconds between downloads to avoid detection.

If downloads fail repeatedly (default: 5 in a row), the bot enters a recovery state where it will pause new downloads and retry every 15 minutes until something succeeds.

## Testing
You can make a file called `test_videos.txt` with a YouTube video on each line; when you start the bot it will enter a test mode to make sure the bot works without GET'ing information from the thps.run API. Once it downloads the videos, it will shutdown.

## Auto-Updating yt-dlp
The Docker entrypoint upgrades yt-dlp on every container start. The bot self-restarts nightly (4:00–4:30 AM UTC) when idle, so yt-dlp stays current without manual intervention.

## Cookie Maintenance
YouTube cookies expire periodically. When downloads start failing with 403 or bot-detection errors, re-export `data/cookies.txt` from your browser and restart the container: `docker compose restart archiver`

## API Expectations
The bot expects a Django REST API with:

- `GET /runs/?needs_archive=true` - returns runs with `id` and `video_url` fields
- `PATCH /runs/{id}/` - accepts `{"archived_url": "..."}` to store the archive link

Auth is via `Authorization: Bearer <API_KEY>` header.

## Configuration
Optional environment variables (defaults in parentheses):

| Variable | Default | Description |
|---|---|---|
| `DELAY_MIN_SECONDS` | `30` | Minimum delay between downloads |
| `DELAY_MAX_SECONDS` | `90` | Maximum delay between downloads |
| `CONSECUTIVE_FAILURE_THRESHOLD` | `5` | Failures before entering recovery mode |
| `DOWNLOAD_RATE_LIMIT` | `5M` | yt-dlp download speed limit |
| `SENTRY_DSN` | empty | Sentry DSN for error reporting |
