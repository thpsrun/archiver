import argparse
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
RESTART_WINDOW_START = dt_time(4, 0)
RESTART_WINDOW_END = dt_time(4, 30)
TEST_VIDEOS_PATH = Path("test_videos.txt")


def _run_id_from_file_name(
    file_name: str,
) -> str | None:
    """Extract the run id from a B2 object name of the form '{run_id}.mp4'.

    Arguments:
        file_name (str): The B2 object name.

    Returns:
        run_id (str | None): The run id, or None if the name is not '{run_id}.mp4'.
    """
    if not file_name.endswith(".mp4"):
        return None
    run_id = file_name[: -len(".mp4")]

    return run_id or None


def _resolve_run(
    run_id: str,
    api: APIClient,
    db: Database,
    config: Config,
) -> Run | None:
    """Resolve a run's source video URL, thps.run API first then local DB.

    Arguments:
        run_id (str): The run to resolve.
        api (APIClient): The thps.run API client.
        db (Database): The local database.
        config (Config): Runtime configuration.
    """
    if not config.skip_api:
        run = api.get_run(run_id)
        if run is not None:
            return run

    video_url = db.get_video_url(run_id)
    if video_url:
        return Run(id=run_id, video_url=video_url)
    return None


def recover_unfinished_uploads(
    db: Database,
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    config: Config,
) -> None:
    """Re-download and re-upload any interrupted B2 large-file uploads.

    Lists the bucket's unfinished large files, groups them by run, resolves each run's source URL,
    cancels its stale state, then re-runs the normal download-to-upload pipeline.

    Arguments:
        db (Database): The local database.
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        config (Config): Runtime configuration.
    """
    try:
        unfinished = uploader.list_unfinished_large_files()
    except Exception as e:
        logger.exception("Unfinished-upload sweep: failed to list partials")
        sentry_sdk.capture_exception(e)
        return

    if not unfinished:
        logger.info("Unfinished-upload sweep: none found")
        return

    by_run: dict[str, list] = {}
    for uf in unfinished:
        run_id = _run_id_from_file_name(uf.file_name)
        if run_id is None:
            logger.warning(
                "Unfinished-upload sweep: skipping unexpected name %s", uf.file_name
            )
            continue
        by_run.setdefault(run_id, []).append(uf)

    logger.info(
        "Unfinished-upload sweep: %d partial(s) across %d run(s)",
        len(unfinished),
        len(by_run),
    )

    recovered = 0
    unresolved = 0
    failed = 0

    for run_id, partials in by_run.items():
        run = _resolve_run(run_id, api, db, config)
        if run is None:
            unresolved += 1
            logger.warning(
                "Unfinished-upload sweep: cannot resolve run %s; leaving %d in place...",
                run_id,
                len(partials),
            )
            sentry_sdk.capture_message(
                f"Unfinished B2 upload for run {run_id} could not be resolved "
                "to a source URL; partial left in place",
                level="warning",
            )
            continue

        for uf in partials:
            try:
                uploader.cancel_large_file(uf.file_id)
            except Exception as e:
                logger.warning(
                    "Unfinished-upload sweep: failed to cancel %s: %s", uf.file_id, e
                )
                sentry_sdk.capture_exception(e)

        logger.info("Unfinished-upload sweep: recovering run %s", run_id)
        success, _ = process_run(run, downloader, uploader, api, db, config)
        if success:
            recovered += 1
        else:
            failed += 1
            logger.warning(
                "Unfinished-upload sweep: run %s did not complete; queued for retry",
                run_id,
            )

    logger.info(
        "Unfinished-upload sweep complete: %d recovered, %d unresolved, %d failed",
        recovered,
        unresolved,
        failed,
    )


def maybe_daily_sweep(
    db: Database,
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    config: Config,
) -> None:
    """Run the unfinished-upload sweep at most once per UTC day.

    Arguments:
        db (Database): The local database.
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        config (Config): Runtime configuration.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    if db.get_meta("last_uf_sweep_date") == today:
        return
    recover_unfinished_uploads(db, downloader, uploader, api, config)
    db.set_meta("last_uf_sweep_date", today)


def init_sentry(
    config: Config,
) -> None:
    """Initialize Sentry error reporting when a DSN is configured.

    Arguments:
        config (Config): Runtime configuration; init is skipped when sentry_dsn is unset.
    """
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
    """Run one submission end to end: skip/backfill checks, download, upload, finalize.

    Arguments:
        run (Run): The run to process (id + source video_url).
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        db (Database): The local database.
        config (Config): Runtime configuration.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("run_id", run.id)
        scope.set_extra("video_url", run.video_url)

        if not config.skip_api:
            current = api.get_run(run.id)
            if current is not None and current.arch_video:
                logger.info("Run %s already has arch_video; skipping download", run.id)
                db.mark_processed(run.id, run.video_url, current.arch_video)
                return True, False

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
    """Retry the oldest queued run to check whether downloads are working again.

    Arguments:
        db (Database): The local database.
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        config (Config): Runtime configuration.
    """
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
    """Poll the API for verified runs and process them forever, with failure recovery.

    Arguments:
        config (Config): Runtime configuration.
        db (Database): The local database.
        api (APIClient): The thps.run API client.
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
    """
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
                    maybe_daily_sweep(db, downloader, uploader, api, config)
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

                if not success and not db.is_skipped(run.id):
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


def load_test_videos(
    path: Path,
) -> list[Run]:
    """Load '{run_id},{video_url}' lines from a test-videos file."""
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
    queue_on_failure: bool = True,
) -> bool:
    """Upload a downloaded file to B2, write the archive URL back, and record the run.

    Arguments:
        run (Run): The run being finalized.
        file_path (str | None): Local path to the downloaded file; None is a no-op.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        db (Database): The local database.
        config (Config): Runtime configuration.
        queue_on_failure (bool): When True, an upload failure re-queues the run for
            retry; force re-uploads pass False to report but not re-queue.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("run_id", run.id)
        scope.set_extra("video_url", run.video_url)

        if not file_path:
            return False

        try:
            archive_url = uploader.upload(file_path, run.id)
        except UploadError as e:
            # Force re-uploads pass queue_on_failure=False: report the failure
            # but don't add the run to the retry queue.
            if queue_on_failure:
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


def _process_videos_with_pipeline(
    pending: list[Run],
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    db: Database,
    config: Config,
    label: str = "Test mode",
) -> tuple[int, int, int]:
    """Download and archive each run in sequence, tallying the outcomes.

    Arguments:
        pending (list[Run]): The runs to process, in order.
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        db (Database): The local database.
        config (Config): Runtime configuration.
        label (str): Human-readable phase label for log lines.

    """
    total = len(pending)
    succeeded = 0
    failed = 0
    skipped = 0

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

        if _upload_and_finalize(
            run,
            result.file_path,
            uploader,
            api,
            db,
            config,
        ):
            succeeded += 1
        else:
            failed += 1

        if i < total:
            delay = random.uniform(
                config.delay_min_seconds,
                config.delay_max_seconds,
            )
            logger.debug("Sleeping for %.1f seconds", delay)
            time.sleep(delay)

    return succeeded, failed, skipped


def _check_cookie_fails(
    db: Database,
) -> bool:
    """Report whether every queued failure looks cookie/auth related.

    Arguments:
        db (Database): The local database.
    """
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
    """Process videos from test_videos.txt with retry passes, bypassing the poll loop.

    Arguments:
        db (Database): The local database.
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        config (Config): Runtime configuration.
    """
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


def _parse_id_list(
    raw: str,
) -> list[str]:
    """Split a comma-separated run-id string into a de-duped, ordered list.

    Arguments:
        raw (str): The raw --forceupload value, e.g. "vid1,vid2,vid1".
    """
    seen: set[str] = set()
    ids: list[str] = []
    for part in raw.split(","):
        run_id = part.strip()
        if run_id and run_id not in seen:
            seen.add(run_id)
            ids.append(run_id)
    return ids


def _force_one(
    run: Run,
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    db: Database,
    config: Config,
) -> bool:
    """Force one run through the pipeline and force uploads it.

    Arguments:
        run (Run): The resolved run (id + source video_url).
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        db (Database): The local database.
        config (Config): Runtime configuration.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("run_id", run.id)
        scope.set_extra("video_url", run.video_url)

        logger.info("Force upload: re-downloading run %s (%s)", run.id, run.video_url)
        result = downloader.download(run.id, run.video_url)

        if not result.success:
            logger.error(
                "Force upload: download failed for run %s: %s",
                run.id,
                result.error_message,
            )
            return False

        if _upload_and_finalize(
            run,
            result.file_path,
            uploader,
            api,
            db,
            config,
            queue_on_failure=False,
        ):
            logger.info("Force upload: run %s re-archived", run.id)
            return True

        logger.error("Force upload: upload/finalize failed for run %s", run.id)
        return False


def force_upload_runs(
    run_ids: list[str],
    downloader: Downloader,
    uploader: Uploader,
    api: APIClient,
    db: Database,
    config: Config,
) -> int:
    """Re-download and re-upload each run id, overwriting any existing archive.

    Arguments:
        run_ids (list[str]): The run ids to re-archive.
        downloader (Downloader): The yt-dlp downloader.
        uploader (Uploader): The B2 uploader.
        api (APIClient): The thps.run API client.
        db (Database): The local database.
        config (Config): Runtime configuration.
    """
    succeeded = 0
    failed = 0

    for run_id in run_ids:
        run = _resolve_run(run_id, api, db, config)
        if run is None:
            failed += 1
            logger.error(
                "Force upload: could not resolve source URL for run %s; skipping",
                run_id,
            )
            continue

        if _force_one(run, downloader, uploader, api, db, config):
            succeeded += 1
        else:
            failed += 1

    logger.info("Force upload complete: %d succeeded, %d failed", succeeded, failed)
    return failed


def parse_args(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """Parse command-line arguments.

    Arguments:
        argv (list[str] | None): Argument vector, or None to use sys.argv.
    """
    parser = argparse.ArgumentParser(prog="archiver")
    parser.add_argument(
        "--forceupload",
        metavar="IDS",
        help=(
            "Comma-separated run IDs to re-download and re-upload, "
            "overwriting any existing archive."
        ),
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    logger.info("Starting thps.run YouTube Archiver Bot")

    config = load_config()
    init_sentry(config)

    db = Database(config)
    api = APIClient(config)
    downloader = Downloader(config)
    uploader = Uploader(config)

    if args.forceupload:
        run_ids = _parse_id_list(args.forceupload)
        if not run_ids:
            logger.error("--forceupload given but no valid run IDs parsed")
            sys.exit(2)
        logger.info(
            "Force upload requested for %d run(s): %s",
            len(run_ids),
            ", ".join(run_ids),
        )
        failed = force_upload_runs(run_ids, downloader, uploader, api, db, config)
        sys.exit(1 if failed else 0)

    if TEST_VIDEOS_PATH.exists():
        logger.info("Found %s, entering test mode", TEST_VIDEOS_PATH)
        run_test_mode(db, downloader, uploader, api, config)
        return

    logger.info("Initialization complete, starting main loop")
    main_loop(config, db, api, downloader, uploader)


if __name__ == "__main__":
    main()
