import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class Run:
    id: str
    video_url: str
    arch_video: str | None = None


class APIError(Exception):
    pass


class APIAuthError(APIError):
    """401 - API key invalid/expired/revoked. Not retryable; stop and alert."""


class APITerminalError(APIError):
    """4xx (400/403/404/422) - terminal for this run; skip and log."""


class APIClient:
    def __init__(
        self,
        config: Config,
    ):
        self.base_url = config.api_base_url
        self.api_key = config.api_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-API-Key": self.api_key or "",
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> requests.Response:
        url = f"{self.base_url}{endpoint}"
        backoff = 1.0

        for attempt in range(max_retries):
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    raise APIError(
                        f"API request failed after {max_retries} attempts: {e}"
                    )
                logger.warning(
                    "API request error (attempt %d/%d): %s. Retrying in %.1fs...",
                    attempt + 1,
                    max_retries,
                    e,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            status = response.status_code

            if status == 401:
                raise APIAuthError(
                    "401 Unauthorized - API key invalid, expired, or revoked"
                )
            if status in (400, 403, 404, 422):
                raise APITerminalError(
                    f"{status} on {method} {endpoint}: {response.text[:200]}"
                )
            if status == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                logger.warning(
                    "429 rate limited on %s %s; honoring Retry-After=%ds",
                    method,
                    endpoint,
                    retry_after,
                )
                time.sleep(retry_after)
                continue
            if status >= 500:
                if attempt == max_retries - 1:
                    raise APIError(
                        f"Server error {status} after {max_retries} attempts"
                    )
                logger.warning(
                    "Server error %d (attempt %d/%d). Retrying in %.1fs...",
                    status,
                    attempt + 1,
                    max_retries,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            return response

        raise APIError("Unexpected retry loop exit")

    def get_run(
        self,
        run_id: str,
    ) -> Run | None:
        """Fetch a single run's current state from the API."""
        try:
            response = self._request("GET", f"/runs/{run_id}")
        except APIAuthError:
            raise
        except APIError as e:
            logger.warning(
                "Could not fetch run %s for pre-download check: %s", run_id, e
            )
            return None

        item = response.json()
        video_url = item.get("video")
        arch_video = item.get("arch_video")

        if not video_url:
            return None

        return Run(id=run_id, video_url=video_url, arch_video=arch_video)

    def get_pending_runs(
        self,
    ) -> list[Run]:
        response = self._request(
            "GET",
            "/runs/all",
            params={"sort": "newest", "status": "verified", "limit": 20},
        )
        data = response.json()

        runs: list[Run] = []
        for item in data:
            run_id = str(item.get("id", ""))
            video_url = item.get("video")
            arch_video = item.get("arch_video")

            if run_id and video_url:
                runs.append(Run(id=run_id, video_url=video_url, arch_video=arch_video))

        return runs

    def update_archive_url(
        self,
        run_id: str,
        archive_url: str,
    ) -> None:
        if len(archive_url) > 200:
            raise APITerminalError(
                f"archive URL for run {run_id} exceeds 200 chars "
                f"({len(archive_url)}); cannot write back"
            )
        self._request(
            "PUT",
            f"/runs/{run_id}",
            json={"arch_video": archive_url},
        )
        logger.info("Updated run %s with arch_video: %s", run_id, archive_url)
