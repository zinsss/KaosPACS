from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("kaospacs.gateway.dicom.queue")

QUEUE_TABLE = "dicom_forward_queue"
QUEUE_STATUSES = ("pending", "forwarding", "completed", "failed", "dead_letter")


@dataclass(frozen=True)
class DicomQueueRow:
    id: int
    sop_instance_uid: str | None
    study_instance_uid: str | None
    accession_number: str | None
    modality: str | None
    file_path: Path
    status: str
    attempts: int
    last_error: str | None
    next_attempt_at: str | None
    created_at: str
    updated_at: str


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


def get_pending_rows(
    db_path: Path,
    *,
    now: datetime | None = None,
    limit: int = 10,
) -> list[DicomQueueRow]:
    due_at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    with sqlite3.connect(f"file:{db_path}?mode=rw", uri=True) as connection:
        _ensure_schema(connection)
        rows = connection.execute(
            f"""
            SELECT
                id,
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
            FROM {QUEUE_TABLE}
            WHERE status = 'pending'
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id
            LIMIT ?
            """,
            (due_at, limit),
        ).fetchall()
    return [_row_from_sqlite(row) for row in rows]


def requeue_due_failed_rows(
    db_path: Path,
    *,
    now: datetime | None = None,
    max_attempts: int,
) -> None:
    due_at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as connection:
        _ensure_schema(connection)
        connection.execute(
            f"""
            UPDATE {QUEUE_TABLE}
            SET status = 'dead_letter',
                updated_at = ?
            WHERE status = 'failed'
              AND attempts >= ?
            """,
            (due_at, max_attempts),
        )
        connection.execute(
            f"""
            UPDATE {QUEUE_TABLE}
            SET status = 'pending',
                updated_at = ?
            WHERE status = 'failed'
              AND attempts < ?
              AND next_attempt_at IS NOT NULL
              AND next_attempt_at <= ?
            """,
            (due_at, max_attempts, due_at),
        )


def _row_from_sqlite(row: sqlite3.Row | tuple[Any, ...]) -> DicomQueueRow:
    return DicomQueueRow(
        id=int(row[0]),
        sop_instance_uid=row[1],
        study_instance_uid=row[2],
        accession_number=row[3],
        modality=row[4],
        file_path=Path(str(row[5])),
        status=str(row[6]),
        attempts=int(row[7]),
        last_error=row[8],
        next_attempt_at=row[9],
        created_at=str(row[10]),
        updated_at=str(row[11]),
    )


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
