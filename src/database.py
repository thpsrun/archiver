import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from .config import Config


@dataclass
class FailedRun:
    id: int
    run_id: str
    video_url: str
    error_message: str | None
    retry_count: int
    last_attempt: datetime | None
    created_at: datetime


@dataclass
class HealthStatus:
    status: str
    consecutive_failures: int
    last_check: datetime


class Database:
    def __init__(
        self,
        config: Config,
    ):
        self.db_path = config.database_path
        self._init_db()

    @contextmanager
    def _connection(
        self,
    ) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(
        self,
    ) -> None:
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS processed_runs (
                    run_id TEXT PRIMARY KEY,
                    video_url TEXT NOT NULL,
                    archived_url TEXT NOT NULL,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS failed_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT UNIQUE NOT NULL,
                    video_url TEXT NOT NULL,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    last_attempt TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS health_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    consecutive_failures INTEGER DEFAULT 0,
                    last_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS skipped_runs (
                    run_id TEXT PRIMARY KEY,
                    video_url TEXT NOT NULL,
                    reason TEXT,
                    skipped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- Initialize health log if empty
                INSERT OR IGNORE INTO health_log (id, status, consecutive_failures)
                VALUES (1, 'healthy', 0);
            """)

    def _exists_in(
        self,
        table: str,
        run_id: str,
    ) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()
            return row is not None

    def get_meta(
        self,
        key: str,
    ) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row is not None else None

    def set_meta(
        self,
        key: str,
        value: str,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def is_processed(
        self,
        run_id: str,
    ) -> bool:
        return self._exists_in("processed_runs", run_id)

    def is_in_queue(
        self,
        run_id: str,
    ) -> bool:
        return self._exists_in("failed_queue", run_id)

    def is_skipped(
        self,
        run_id: str,
    ) -> bool:
        return self._exists_in("skipped_runs", run_id)

    def mark_skipped(
        self,
        run_id: str,
        video_url: str,
        reason: str | None = None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO skipped_runs (run_id, video_url, reason)
                VALUES (?, ?, ?)
                """,
                (run_id, video_url, reason),
            )
            conn.execute("DELETE FROM failed_queue WHERE run_id = ?", (run_id,))

    def mark_processed(
        self,
        run_id: str,
        video_url: str,
        archived_url: str,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_runs (run_id, video_url, archived_url)
                VALUES (?, ?, ?)
                """,
                (run_id, video_url, archived_url),
            )
            conn.execute("DELETE FROM failed_queue WHERE run_id = ?", (run_id,))

    def add_to_queue(
        self,
        run_id: str,
        video_url: str,
        error_message: str | None = None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO failed_queue (run_id, video_url, error_message, last_attempt)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(run_id) DO UPDATE SET
                    error_message = excluded.error_message,
                    retry_count = retry_count + 1,
                    last_attempt = CURRENT_TIMESTAMP
                """,
                (run_id, video_url, error_message),
            )

    def get_queue(
        self,
        limit: int = 10,
    ) -> list[FailedRun]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, video_url, error_message, retry_count,
                       last_attempt, created_at
                FROM failed_queue
                ORDER BY last_attempt ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                FailedRun(
                    id=row["id"],
                    run_id=row["run_id"],
                    video_url=row["video_url"],
                    error_message=row["error_message"],
                    retry_count=row["retry_count"],
                    last_attempt=(
                        datetime.fromisoformat(row["last_attempt"])
                        if row["last_attempt"]
                        else None
                    ),
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            ]

    def get_queue_errors(
        self,
    ) -> list[str | None]:
        with self._connection() as conn:
            rows = conn.execute("SELECT error_message FROM failed_queue").fetchall()
            return [row["error_message"] for row in rows]

    def get_queue_count(
        self,
    ) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM failed_queue").fetchone()
            return row["cnt"]

    def remove_from_queue(
        self,
        run_id: str,
    ) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM failed_queue WHERE run_id = ?", (run_id,))

    def get_health(
        self,
    ) -> HealthStatus:
        with self._connection() as conn:
            row = conn.execute("""
                SELECT status, consecutive_failures, last_check
                FROM health_log WHERE id = 1
                """).fetchone()
            return HealthStatus(
                status=row["status"],
                consecutive_failures=row["consecutive_failures"],
                last_check=datetime.fromisoformat(row["last_check"]),
            )

    def update_health(
        self,
        status: str,
        consecutive_failures: int,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE health_log
                SET status = ?, consecutive_failures = ?, last_check = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (status, consecutive_failures),
            )

    def increment_failures(
        self,
    ) -> int:
        with self._connection() as conn:
            conn.execute("""
                UPDATE health_log
                SET consecutive_failures = consecutive_failures + 1,
                    last_check = CURRENT_TIMESTAMP
                WHERE id = 1
                """)
            row = conn.execute(
                "SELECT consecutive_failures FROM health_log WHERE id = 1"
            ).fetchone()
            return row["consecutive_failures"]

    def reset_failures(
        self,
    ) -> None:
        with self._connection() as conn:
            conn.execute("""
                UPDATE health_log
                SET status = 'healthy', consecutive_failures = 0,
                    last_check = CURRENT_TIMESTAMP
                WHERE id = 1
                """)
