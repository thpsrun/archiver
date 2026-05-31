import concurrent.futures
import logging
import random
import sys
import time
from datetime import datetime, timezone
from datetime import time as dt_time
from pathlib import Path

import sentry_sdk

from src.api_client import APIAuthError, APIClient, APIError, Run
from src.config import Config, load_config
from src.database import Database
from src.downloader import Downloader, is_cookie_related_failure
from src.uploader import Uploader, UploadError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RECOVERY_WAIT_MINUTES = 15
RESTART_WINDOW_START = dt_time(4, 0)  # 4:00 AM UTC
RESTART_WINDOW_END = dt_time(4, 30)  # 4:30 AM UTC


def init_sentry(
    config: Config,
) -> None:
    if config.sentry_dsn:
        sentry_sdk.init(
            dsn=config.sentry_dsn,
            traces_sample_rate=0.1,
            environment="production",
        )
        logger.info("Sentry initialized")
    else:
        logger.warning("Sentry DSN not configured, error reporting disabled")


def process_run(
    run: Run,
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    db: Database,
    config: Config,
) -> tuple[bool, bool]:
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("run_id", run.id)
        scope.set_extra("video_url", run.video_url)

        existing = uploader.get_existing_file(run.id)
        if existing is not None:
            archive_url = uploader.build_archive_url(run.id, existing)
            logger.info(
                "Run %s already in B2; backfilling arch_video (no re-upload)",
                run.id,
            )
            if not config.skip_api:
                try:
                    api.update_archive_url(run.id, archive_url)
                except APIAuthError:
                    raise
                except APIError as e:
                    logger.error("Backfill update failed for run %s: %s", run.id, e)
                    sentry_sdk.capture_exception(e)
            db.mark_processed(run.id, run.video_url, archive_url)
            return True, False

        result = downloader.download(run.id, run.video_url)

        if not result.success:
            if result.is_permanent_failure:
                logger.warning(
                    "Permanent failure for run %s, skipping: %s",
                    run.id,
                    result.error_message,
                )
                db.mark_skipped(run.id, run.video_url, result.error_message)
                return False, False

            db.add_to_queue(run.id, run.video_url, result.error_message)
            if result.is_youtube_blocked:
                sentry_sdk.capture_message(
                    f"YouTube blocking detected for run {run.id}",
                    level="warning",
                )
            return False, result.is_youtube_blocked

        if _upload_and_finalize(
            run,
            result.file_path,
            uploader,
            api,
            db,
            config,
        ):
            db.reset_failures()
            return True, False

        return False, False


def attempt_recovery(
    db: Database,
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    config: Config,
) -> bool:
    queue = db.get_queue(limit=1)
    if not queue:
        logger.info("Recovery: no items in queue, resuming...")
        db.reset_failures()
        return True

    if _check_cookie_fails(db):
        queue_count = db.get_queue_count()
        logger.error(
            "Recovery: all %d failure(s) are cookie/auth related - "
            "cookies likely need refreshing",
            queue_count,
        )
        sentry_sdk.capture_message(
            f"Recovery mode: all {queue_count} failure(s) are cookie/auth related. "
            "Cookies need refreshing.",
            level="error",
        )

    item = queue[0]
    logger.info("Recovery: attempting retry for run %s", item.run_id)

    run = Run(id=item.run_id, video_url=item.video_url)
    success, _ = process_run(run, downloader, uploader, api, db, config)

    if success:
        logger.info("Recovery succeeded, resuming normal operation")
        db.reset_failures()
        return True

    logger.warning("Recovery failed, will retry in %d minutes", RECOVERY_WAIT_MINUTES)
    return False


def main_loop(
    config: Config,
    db: Database,
    api: APIClient,
    downloader: Downloader,
    uploader: Uploader,
) -> None:
    while True:
        try:
            health = db.get_health()

            if health.consecutive_failures >= config.consecutive_failure_threshold:
                logger.warning(
                    "YouTube appears broken (%d consecutive failures), entering recovery...",
                    health.consecutive_failures,
                )
                db.update_health("broken", health.consecutive_failures)

                if not attempt_recovery(db, downloader, uploader, api, config):
                    logger.info(
                        "Sleeping for %d minutes before next recovery attempt",
                        RECOVERY_WAIT_MINUTES,
                    )
                    time.sleep(RECOVERY_WAIT_MINUTES * 60)
                continue

            try:
                runs = api.get_pending_runs()
            except APIError as e:
                logger.error("Failed to fetch pending runs: %s", e)
                sentry_sdk.capture_exception(e)
                time.sleep(60)
                continue

            if runs:
                last_seen = db.get_meta("last_seen_run_id")
                if runs[0].id != last_seen:
                    db.set_meta("last_seen_run_id", runs[0].id)
                    logger.debug("New newest run id: %s", runs[0].id)

            pending_runs = [
                r
                for r in runs
                if not r.arch_video
                and not db.is_processed(r.id)
                and not db.is_in_queue(r.id)
                and not db.is_skipped(r.id)
            ]

            if not pending_runs:
                logger.debug("No new runs to process")

                now = datetime.now(timezone.utc).time()
                if RESTART_WINDOW_START <= now <= RESTART_WINDOW_END:
                    logger.info(
                        "Maintenance window, no work pending - restarting for updates"
                    )
                    sys.exit(0)
            else:
                logger.info("Found %d new runs to process", len(pending_runs))

            for run in pending_runs:
                success, is_blocked = process_run(
                    run, downloader, uploader, api, db, config
                )

                if not success:
                    if not db.is_skipped(run.id):
                        failures = db.increment_failures()
                        logger.warning(
                            "Failure count: %d/%d",
                            failures,
                            config.consecutive_failure_threshold,
                        )

                        if is_blocked:
                            sentry_sdk.capture_message(
                                f"YouTube blocking detected, failure count: {failures}",
                                level="error",
                            )

                if run != pending_runs[-1]:
                    delay = random.uniform(
                        config.delay_min_seconds,
                        config.delay_max_seconds,
                    )
                    logger.debug("Sleeping for %.1f seconds", delay)
                    time.sleep(delay)

            queue = db.get_queue(limit=1)
            if queue and pending_runs:
                item = queue[0]
                logger.info("Retrying queued run: %s", item.run_id)
                run = Run(id=item.run_id, video_url=item.video_url)
                process_run(run, downloader, uploader, api, db, config)

            delay = random.uniform(
                config.delay_min_seconds,
                config.delay_max_seconds,
            )
            time.sleep(delay)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except APIAuthError as e:
            logger.error("API auth failed (401) - stopping the bot: %s", e)
            sentry_sdk.capture_exception(e)
            sys.exit(1)
        except Exception as e:
            logger.exception("Unexpected error in main loop")
            sentry_sdk.capture_exception(e)
            time.sleep(60)


TEST_VIDEOS_PATH = Path("test_videos.txt")


def load_test_videos(
    path: Path,
) -> list[Run]:
    runs: list[Run] = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," not in line:
                logger.warning(
                    "Skipping invalid line %d (no comma delimiter): %s", i, line
                )
                continue
            run_id, video_url = line.split(",", maxsplit=1)
            run_id = run_id.strip()
            video_url = video_url.strip()
            if not run_id or not video_url:
                logger.warning(
                    "Skipping invalid line %d (empty id or url): %s", i, line
                )
                continue
            runs.append(Run(id=run_id, video_url=video_url))
    return runs


def _upload_and_finalize(
    run: Run,
    file_path: str | None,
    uploader: Uploader,
    api: APIClient,
    db: Database,
    config: Config,
) -> bool:
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("run_id", run.id)
        scope.set_extra("video_url", run.video_url)

        if file_path:
            try:
                archive_url = uploader.upload(file_path, run.id)
            except UploadError as e:
                db.add_to_queue(run.id, run.video_url, str(e))
                sentry_sdk.capture_exception(e)
                return False
            finally:
                uploader.cleanup(file_path)

            if not config.skip_api:
                try:
                    api.update_archive_url(run.id, archive_url)
                except APIAuthError:
                    raise
                except APIError as e:
                    logger.error("API update failed for run %s: %s", run.id, e)
                    sentry_sdk.capture_exception(e)
                    db.mark_processed(run.id, run.video_url, archive_url)
                    return False

            db.mark_processed(run.id, run.video_url, archive_url)
            logger.info("Successfully processed run %s", run.id)
            return True
        else:
            return False


def _process_videos_with_pipeline(
    pending: list[Run],
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    db: Database,
    config: Config,
    label: str = "Test mode",
) -> tuple[int, int, int]:
    total = len(pending)
    succeeded = 0
    failed = 0
    skipped = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        upload_future: concurrent.futures.Future[bool] | None = None

        def results() -> None:
            nonlocal succeeded, failed, upload_future
            if upload_future is not None:
                if upload_future.result():
                    succeeded += 1
                else:
                    failed += 1
                upload_future = None

        for i, run in enumerate(pending, start=1):
            logger.info(
                "[%d/%d] %s - processing run %s: %s",
                i,
                total,
                label,
                run.id,
                run.video_url,
            )

            with sentry_sdk.new_scope() as scope:
                scope.set_tag("run_id", run.id)
                scope.set_extra("video_url", run.video_url)
                result = downloader.download(run.id, run.video_url)

            if not result.success:
                if result.is_permanent_failure:
                    logger.warning(
                        "[%d/%d] Permanent failure for run %s, skipping: %s",
                        i,
                        total,
                        run.id,
                        result.error_message,
                    )
                    db.mark_skipped(run.id, run.video_url, result.error_message)
                    skipped += 1
                else:
                    db.add_to_queue(run.id, run.video_url, result.error_message)
                    if result.is_youtube_blocked:
                        sentry_sdk.capture_message(
                            f"YouTube blocking detected for run {run.id}",
                            level="warning",
                        )
                    failed += 1
                continue

            results()

            upload_future = executor.submit(
                _upload_and_finalize,
                run,
                result.file_path,
                uploader,
                api,
                db,
                config,
            )

            if i < total:
                delay = random.uniform(
                    config.delay_min_seconds,
                    config.delay_max_seconds,
                )
                logger.debug("Sleeping for %.1f seconds", delay)
                time.sleep(delay)

        results()

    return succeeded, failed, skipped


def _check_cookie_fails(
    db: Database,
) -> bool:
    errors = db.get_queue_errors()
    if not errors:
        return False
    return all(is_cookie_related_failure(err) for err in errors)


def run_test_mode(
    db: Database,
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    config: Config,
) -> None:
    runs = load_test_videos(TEST_VIDEOS_PATH)
    logger.info("Test mode: loaded %d video(s) from %s", len(runs), TEST_VIDEOS_PATH)

    pending = [r for r in runs if not db.is_processed(r.id) and not db.is_skipped(r.id)]

    if not pending:
        logger.info("Test mode: all videos already processed or skipped")
        return

    logger.info("Test mode: %d video(s) to process", len(pending))

    succeeded, _, skipped = _process_videos_with_pipeline(
        pending,
        downloader,
        uploader,
        api,
        db,
        config,
        label="Initial pass",
    )
    total_recovered = 0

    for retry_pass in range(config.retry_passes):
        queue_count = db.get_queue_count()
        if queue_count == 0:
            logger.info("Retry: no failures in queue, skipping retries")
            break

        if _check_cookie_fails(db):
            logger.error(
                "Retry: all %d failure(s) are cookie/auth related - "
                "cookies need refreshing, skipping further retries",
                queue_count,
            )
            sentry_sdk.capture_message(
                f"All {queue_count} test mode failure(s) are cookie/auth related. "
                "Cookies need refreshing.",
                level="error",
            )
            break

        delay = config.retry_delay_seconds * (2**retry_pass)
        logger.info(
            "Retry pass %d/%d: %d failure(s) in queue, waiting %ds before retry",
            retry_pass + 1,
            config.retry_passes,
            queue_count,
            delay,
        )
        time.sleep(delay)

        queue_items = db.get_queue(limit=1000)
        retry_runs = [
            Run(id=item.run_id, video_url=item.video_url) for item in queue_items
        ]

        r_succeeded, _, r_skipped = _process_videos_with_pipeline(
            retry_runs,
            downloader,
            uploader,
            api,
            db,
            config,
            label=f"Retry pass {retry_pass + 1}",
        )
        total_recovered += r_succeeded
        skipped += r_skipped

    final_queue = db.get_queue_count()
    logger.info(
        "Test mode complete: %d succeeded (%d recovered), %d failed, %d skipped",
        succeeded + total_recovered,
        total_recovered,
        final_queue,
        skipped,
    )


def main() -> None:
    logger.info("Starting YouTube Archiver Bot")

    config = load_config()
    init_sentry(config)

    db = Database(config)
    api = APIClient(config)
    downloader = Downloader(config)
    uploader = Uploader(config)

    if TEST_VIDEOS_PATH.exists():
        logger.info("Found %s, entering test mode", TEST_VIDEOS_PATH)
        run_test_mode(db, downloader, uploader, api, config)
        return

    logger.info("Initialization complete, starting main loop")
    main_loop(config, db, api, downloader, uploader)


if __name__ == "__main__":
    main()
