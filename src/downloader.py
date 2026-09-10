import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.config import Config

logger = logging.getLogger(__name__)

# Auth / bot-detection signals that a fresh cookie export can clear.
COOKIE_PATTERNS = [
    "Sign in to confirm",
    "Sign in to confirm your age",
    "bot detection",
    "confirm you're not a bot",
]

# Rate-limiting signals: transient, and NOT fixed by refreshing cookies.
RATE_LIMIT_PATTERNS = [
    "HTTP Error 429",
    "too many requests",
]

# Any of these in yt-dlp output means YouTube is actively blocking the download.
BLOCKING_PATTERNS = COOKIE_PATTERNS + RATE_LIMIT_PATTERNS

PROFILE_FALLBACK_PATTERNS = [
    "Requested format is not available",
    "HTTP Error 403",
    "The page needs to be reloaded",
]

PERMANENT_FAILURE_PATTERNS = [
    "This video is unavailable",
    "Video unavailable",
    "Private video",
    "This video has been removed",
    "This video is no longer available",
    "This video has been removed for violating YouTube's Terms of Service",
    "This video has been removed by the uploader",
    "This video is private",
    "copyright claim",
    "The uploader has not made this video available in your country",
    "account associated with this video has been terminated",
    "This video does not exist",
    "is not a valid URL",
]


def is_cookie_related_failure(
    error_message: str | None,
) -> bool:
    if not error_message:
        return False
    msg_lower = error_message.lower()
    return any(pattern.lower() in msg_lower for pattern in COOKIE_PATTERNS)


@dataclass
class DownloadResult:
    success: bool
    file_path: str | None
    error_message: str | None
    is_youtube_blocked: bool
    is_permanent_failure: bool


class Downloader:
    def __init__(
        self,
        config: Config,
    ):
        self.config = config
        self.downloads_dir = Path(config.downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    def download(
        self,
        run_id: str,
        video_url: str,
    ) -> DownloadResult:
        output_path = self.downloads_dir / f"{run_id}.mp4"
        profiles = self.config.ytdlp_client_profiles or ["default"]

        for index, client in enumerate(profiles):
            result, output = self._attempt_download(
                run_id,
                video_url,
                output_path,
                client,
            )

            if result.success:
                return result

            # it's a permanent failure, so just skip attempting to use other profiles.
            if result.is_permanent_failure or result.is_youtube_blocked:
                return result

            is_last = index == len(profiles) - 1
            if is_last or not self._should_try_next_profile(output):
                return result

            logger.info(
                "Client %s failed for run %s (%s); trying next profile",
                client,
                run_id,
                result.error_message,
            )

        raise RuntimeError("no yt-dlp client profiles configured")

    def _attempt_download(
        self,
        run_id: str,
        video_url: str,
        output_path: Path,
        client: str,
    ) -> tuple[DownloadResult, str]:
        if output_path.exists():
            output_path.unlink()

        cmd = [
            "yt-dlp",
            "--config-location",
            self.config.ytdlp_config_path,
            "--extractor-args",
            f"youtube:player_client={client}",
            "--limit-rate",
            self.config.download_rate_limit,
            "--output",
            str(output_path),
            video_url,
        ]

        logger.info(
            "Starting download for run %s with client %s: %s",
            run_id,
            client,
            video_url,
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=7200,  # 2h: long speedruns (multi-GB HLS) exceed 1h
            )

            combined_output = result.stdout + result.stderr

            if result.returncode == 0 and output_path.exists():
                logger.info("Download successful: %s", output_path)
                return (
                    DownloadResult(
                        success=True,
                        file_path=str(output_path),
                        error_message=None,
                        is_youtube_blocked=False,
                        is_permanent_failure=False,
                    ),
                    combined_output,
                )

            is_blocked = self._is_blocked(combined_output)
            is_permanent = self._is_perma_fail(combined_output)
            error_msg = (
                self._on_error(combined_output) or f"Exit code: {result.returncode}"
            )

            logger.error(
                "Download failed for run %s (client %s): %s (blocked=%s, permanent=%s)",
                run_id,
                client,
                error_msg,
                is_blocked,
                is_permanent,
            )

            return (
                DownloadResult(
                    success=False,
                    file_path=None,
                    error_message=error_msg,
                    is_youtube_blocked=is_blocked,
                    is_permanent_failure=is_permanent,
                ),
                combined_output,
            )
        except subprocess.TimeoutExpired:
            logger.error("Download timed out for run %s (client %s)", run_id, client)
            return (
                DownloadResult(
                    success=False,
                    file_path=None,
                    error_message="Download timed out after 2 hours",
                    is_youtube_blocked=False,
                    is_permanent_failure=False,
                ),
                "",
            )
        except Exception as e:
            logger.exception(
                "Unexpected error downloading run %s (client %s)", run_id, client
            )
            return (
                DownloadResult(
                    success=False,
                    file_path=None,
                    error_message=str(e),
                    is_youtube_blocked=False,
                    is_permanent_failure=False,
                ),
                "",
            )

    @staticmethod
    def _should_try_next_profile(
        output: str,
    ) -> bool:
        output_lower = output.lower()
        return any(
            pattern.lower() in output_lower for pattern in PROFILE_FALLBACK_PATTERNS
        )

    @staticmethod
    def _is_blocked(
        output: str,
    ) -> bool:
        output_lower = output.lower()
        return any(pattern.lower() in output_lower for pattern in BLOCKING_PATTERNS)

    @staticmethod
    def _is_perma_fail(
        output: str,
    ) -> bool:
        output_lower = output.lower()
        return any(
            pattern.lower() in output_lower for pattern in PERMANENT_FAILURE_PATTERNS
        )

    def _on_error(
        self,
        output: str,
    ) -> str | None:
        lines = output.strip().split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line and "error" in line.lower():
                return line[:500]
        for line in reversed(lines):
            if line.strip():
                return line.strip()[:500]
        return None
