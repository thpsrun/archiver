# thps.run Archiver
Specialized bot (within a Docker container) that automatically archives YouTube videos once they are approved in the thps.run API. It will continually poll thps.run for submissions, download them via the `yt-dlp` library, upload them to a BackBlaze bucket, and update the submission with the archived URL.

As such, this project is mainly meant for thps.run support, but it is open-source if you need ideas of your own!

## Requirements
- Docker and Docker Compose
- A Google/YouTube account for cookie authentication
- BackBlaze B2 bucket
- A Django API serving run data

## Setup

1. **Clone and configure environment**

   `cp .env.example .env`

   * Fill in `.env` with your credentials:
     - `API_BASE_URL` / `API_KEY` - your Django API endpoint and auth key
     - `B2_APPLICATION_KEY_ID` / `B2_APPLICATION_KEY` / `B2_BUCKET_NAME` - BackBlaze B2 credentials
     - `SENTRY_DSN` - optional, leave empty to disable error reporting

2. **Export YouTube cookies**

    Log into a burner YouTube account, then use:
    `./yt-dlp --cookies-from-browser chrome --cookies cookies.txt`

   > [!TIP]
   > Use a burner Google account for this. YouTube may flag the account for automated activity, which is very bad if you are using your real account.

3. **Start the bot**

    `docker compose up --build -d`

4. **View Bot Logs**

    `docker compose logs -f archiver`

5. **Execute Commands Within Container**

    `docker exec -it youtube-archiver /bin/bash`

## Testing
You can make a file called `test_videos.txt` with a YouTube video on each line; when you start the bot it will enter a test mode to make sure the bot works without GET'ing information from the thps.run API. Once it downloads the videos, it will shutdown.

## Auto-Updating yt-dlp
The Docker entrypoint upgrades yt-dlp on every container start. The bot self-restarts nightly (4:00–4:30 AM UTC) when idle, so yt-dlp stays current without manual intervention.

## Cookie Maintenance
YouTube cookies expire periodically. When downloads start failing with 403 or bot-detection errors, re-export `data/cookies.txt` from your browser and restart the container: `docker compose restart archiver`