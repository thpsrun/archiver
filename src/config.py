import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Config:
    # Django API
    api_base_url: str | None
    api_key: str | None

    # BackBlaze B2
    b2_application_key_id: str
    b2_application_key: str
    b2_bucket_name: str

    # Testing
    skip_api: bool

    # Sentry (optional)
    sentry_dsn: str | None

    # Archive CDN URL (optional)
    archive_url: str | None

    # Throttling
    download_rate_limit: str
    delay_min_seconds: int
    delay_max_seconds: int
    consecutive_failure_threshold: int

    # Retry
    retry_passes: int
    retry_delay_seconds: int

    # Paths
    data_dir: str
    cookies_path: str
    downloads_dir: str
    database_path: str
    ytdlp_config_path: str


def load_config() -> Config:
    load_dotenv()

    def require_env(
        name: str,
    ) -> str:
        value = os.getenv(name)
        if not value:
            raise ValueError(f"Required environment variable {name} is not set")
        return value

    data_dir = os.getenv("DATA_DIR", "/app/data")
    skip_api = os.getenv("SKIP_API", "false").lower() in ("true", "1", "yes")

    if skip_api:
        api_base_url = None
        api_key = None
    else:
        api_base_url = require_env("API_BASE_URL").rstrip("/")
        api_key = require_env("API_KEY")

    return Config(
        # thps.run API
        api_base_url=api_base_url,
        api_key=api_key,
        # BackBlaze Archiving
        b2_application_key_id=require_env("B2_APPLICATION_KEY_ID"),
        b2_application_key=require_env("B2_APPLICATION_KEY"),
        b2_bucket_name=require_env("B2_BUCKET_NAME"),
        # Testing
        skip_api=skip_api,
        # Sentry
        sentry_dsn=os.getenv("SENTRY_DSN"),
        # Archive URL
        archive_url=os.getenv("ARCHIVE_URL"),
        # Throttling and Data Limiting
        download_rate_limit=os.getenv("DOWNLOAD_RATE_LIMIT", "5M"),
        delay_min_seconds=int(os.getenv("DELAY_MIN_SECONDS", "30")),
        delay_max_seconds=int(os.getenv("DELAY_MAX_SECONDS", "90")),
        consecutive_failure_threshold=int(
            os.getenv("CONSECUTIVE_FAILURE_THRESHOLD", "5")
        ),
        # Retry
        retry_passes=int(os.getenv("RETRY_PASSES", "3")),
        retry_delay_seconds=int(os.getenv("RETRY_DELAY_SECONDS", "120")),
        # Paths
        data_dir=data_dir,
        cookies_path=os.path.join(data_dir, "cookies.txt"),
        downloads_dir=os.path.join(data_dir, "downloads"),
        database_path=os.path.join(data_dir, "archiver.db"),
        ytdlp_config_path=os.getenv("YTDLP_CONFIG_PATH", "/app/yt-dlp.conf"),
    )
