from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("kaospacs.gateway.dicom.queue")

QUEUE_TABLE = "dicom_forward_queue"
QUEUE_STATUSES = ("pending", "forwarding", "completed", "failed", "dead_letter")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def init_queue_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        _ensure_schema(connection)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
            id INTEGER PRIMARY KEY,
            sop_instance_uid TEXT,
            study_instance_uid TEXT,
            accession_number TEXT,
            modality TEXT,
            file_path TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            next_attempt_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{QUEUE_TABLE}_status_next_attempt
        ON {QUEUE_TABLE} (status, next_attempt_at)
        """
    )


def enqueue_stored_dataset(db_path: Path, dataset: Any, file_path: Path) -> int:
    now = _now()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        _ensure_schema(connection)
        cursor = connection.execute(
            f"""
            INSERT INTO {QUEUE_TABLE} (
                sop_instance_uid,
                study_instance_uid,
                accession_number,
                modality,
                file_path,
                status,
                attempts,
                last_error,
                next_attempt_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, ?)
            """,
            (
                _text(getattr(dataset, "SOPInstanceUID", "")) or None,
                _text(getattr(dataset, "StudyInstanceUID", "")) or None,
                _text(getattr(dataset, "AccessionNumber", "")) or None,
                _text(getattr(dataset, "Modality", "")) or None,
                str(file_path),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def get_queue_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{db_path}?mode=rw", uri=True) as connection:
        _ensure_schema(connection)
        rows = connection.execute(
            f"""
            SELECT status, COUNT(*)
            FROM {QUEUE_TABLE}
            GROUP BY status
            """
        ).fetchall()
    counts = {status: 0 for status in QUEUE_STATUSES}
    for status, count in rows:
        if status in counts:
            counts[status] = int(count)
    return counts


def mark_forwarding(db_path: Path, queue_id: int) -> None:
    _mark_status(db_path, queue_id, "forwarding", increment_attempts=True)


def mark_completed(db_path: Path, queue_id: int) -> None:
    _mark_status(db_path, queue_id, "completed", last_error=None)


def mark_failed(
    db_path: Path,
    queue_id: int,
    *,
    last_error: str | None = None,
    next_attempt_at: str | None = None,
) -> None:
    _mark_status(
        db_path,
        queue_id,
        "failed",
        last_error=last_error,
        next_attempt_at=next_attempt_at,
    )


def mark_dead_letter(
    db_path: Path,
    queue_id: int,
    *,
    last_error: str | None = None,
) -> None:
    _mark_status(db_path, queue_id, "dead_letter", last_error=last_error)


def _mark_status(
    db_path: Path,
    queue_id: int,
    status: str,
    *,
    increment_attempts: bool = False,
    last_error: str | None = None,
    next_attempt_at: str | None = None,
) -> None:
    if status not in QUEUE_STATUSES:
        raise ValueError(f"invalid queue status: {status}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        _ensure_schema(connection)
        if increment_attempts:
            connection.execute(
                f"""
                UPDATE {QUEUE_TABLE}
                SET status = ?,
                    attempts = attempts + 1,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, last_error, next_attempt_at, _now(), queue_id),
            )
        else:
            connection.execute(
                f"""
                UPDATE {QUEUE_TABLE}
                SET status = ?,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, last_error, next_attempt_at, _now(), queue_id),
            )
