import logging
import time
from pathlib import Path

from b2sdk.v2 import (
    AbstractProgressListener,
    B2Api,
    Bucket,
    DownloadVersion,
    FileVersion,
    InMemoryAccountInfo,
)
from b2sdk.v2.exception import FileNotPresent

from src.config import Config

logger = logging.getLogger(__name__)


def _format_bytes(
    num_bytes: int | float,
) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def _format_bitrate(
    bytes_per_sec: float,
) -> str:
    bits_per_sec = bytes_per_sec * 8
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if abs(bits_per_sec) < 1000:
            return f"{bits_per_sec:.1f} {unit}"
        bits_per_sec /= 1000
    return f"{bits_per_sec:.1f} Tbps"


def _format_time(
    seconds: float,
) -> str:
    if seconds < 0:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m {secs}s"


class UploadProgressListener(AbstractProgressListener):
    LOG_INTERVAL_SECONDS = 30

    def __init__(
        self,
        file_name: str,
        total_bytes: int,
    ):
        super().__init__()
        self.file_name = file_name
        self.total_bytes = total_bytes
        self._start_time: float | None = None
        self._last_log_time: float = 0

    def set_total_bytes(  # type: ignore
        self,
        total_bytes: int,
    ) -> None:
        self.total_bytes = total_bytes

    def bytes_completed(
        self,
        byte_count: int,
    ) -> None:
        now = time.monotonic()

        if self._start_time is None:
            self._start_time = now
            self._last_log_time = now
            return

        elapsed_since_log = now - self._last_log_time
        if elapsed_since_log < self.LOG_INTERVAL_SECONDS:
            return

        self._last_log_time = now
        elapsed_total = now - self._start_time

        if elapsed_total <= 0:
            return

        bytes_per_sec = byte_count / elapsed_total
        remaining_bytes = self.total_bytes - byte_count
        time_remaining = remaining_bytes / bytes_per_sec if bytes_per_sec > 0 else -1

        logger.info(
            "Uploading: %s | [%s/%s] @ %s | %s remaining",
            self.file_name,
            _format_bytes(byte_count),
            _format_bytes(self.total_bytes),
            _format_bitrate(bytes_per_sec),
            _format_time(time_remaining),
        )

    def close(
        self,
    ) -> None:
        pass


class UploadError(Exception):
    pass


class Uploader:
    def __init__(
        self,
        config: Config,
    ):
        self.config = config
        self._api: B2Api | None = None
        self._bucket: Bucket | None = None

    def _get_api(
        self,
    ) -> B2Api:
        if self._api is None:
            info = InMemoryAccountInfo()
            self._api = B2Api(info)  # type: ignore
            self._api.authorize_account(
                "production",
                self.config.b2_application_key_id,
                self.config.b2_application_key,
            )
        return self._api

    def _get_bucket(
        self,
    ) -> Bucket:
        if self._bucket is None:
            api = self._get_api()
            self._bucket = api.get_bucket_by_name(self.config.b2_bucket_name)  # type: ignore
        return self._bucket  # type: ignore

    def get_existing_file(
        self,
        run_id: str,
    ) -> DownloadVersion | None:
        file_name = f"{run_id}.mp4"
        try:
            return self._get_bucket().get_file_info_by_name(file_name)  # type: ignore
        except FileNotPresent:
            return None

    def build_archive_url(
        self,
        run_id: str,
        file_ver: DownloadVersion | FileVersion,
    ) -> str:
        if self.config.archive_url:
            return f"{self.config.archive_url}/{run_id}.mp4"
        return self._get_api().get_download_url_for_fileid(file_ver.id_)  # type: ignore

    def upload(
        self,
        file_path: str,
        run_id: str,
    ) -> str:
        path = Path(file_path)
        if not path.exists():
            raise UploadError(f"File does not exist: {file_path}")

        b2_file_name = f"{run_id}.mp4"

        file_size = path.stat().st_size
        logger.info(
            "Uploading %s to B2 as %s (%s)",
            file_path,
            b2_file_name,
            _format_bytes(file_size),
        )

        try:
            bucket = self._get_bucket()
            progress_listener = UploadProgressListener(
                file_name=b2_file_name,
                total_bytes=file_size,
            )
            file_ver = bucket.upload_local_file(
                local_file=str(path),
                file_name=b2_file_name,
                progress_listener=progress_listener,
            )

            download_url = self.build_archive_url(run_id, file_ver)

            logger.info("Upload successful: %s", download_url)
            return download_url
        except Exception as e:
            logger.exception("Upload failed for %s", file_path)
            raise UploadError(f"Failed to upload to B2: {e}") from e

    def cleanup(
        self,
        file_path: str,
    ) -> None:
        try:
            path = Path(file_path)
            if path.exists():
                path.unlink()
                logger.debug("Cleaned up local file: %s", file_path)
        except Exception as e:
            logger.warning("Failed to clean up file %s: %s", file_path, e)
