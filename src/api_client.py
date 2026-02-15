import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class Run:
    id: str
    video_url: str


class APIError(Exception):
    pass


class APIClient:
    def __init__(self, config: Config):
        self.base_url = config.api_base_url
        self.api_key = config.api_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
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
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    raise APIError(
                        f"API request failed after {max_retries} attempts: {e}"
                    )

                logger.warning(
                    "API request failed (attempt %d/%d): %s. Retrying in %.1fs...",
                    attempt + 1,
                    max_retries,
                    e,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

        raise APIError("Unexpected retry loop exit")

    def get_pending_runs(self) -> list[Run]:
        response = self._request("GET", "/runs/", params={"needs_archive": "true"})
        data = response.json()

        runs = []
        for item in data.get("results", data) if isinstance(data, dict) else data:
            run_id = str(item.get("id", ""))
            video_url = item.get("video_url", "")

            if run_id and video_url:
                runs.append(Run(id=run_id, video_url=video_url))

        return runs

    def update_archive_url(self, run_id: str, archive_url: str) -> None:
        self._request(
            "PATCH",
            f"/runs/{run_id}/",
            json={"archived_url": archive_url},
        )
        logger.info("Updated run %s with archive URL: %s", run_id, archive_url)
