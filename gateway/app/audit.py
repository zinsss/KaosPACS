from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path


LOGGER = logging.getLogger("kaospacs.gateway.audit")

AUDIT_TABLE = "gateway_events"


def init_audit_db(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            _ensure_schema(connection)
    except Exception as error:
        LOGGER.warning("Gateway audit DB initialization failed path=%s error=%s", path, error)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
            id INTEGER PRIMARY KEY,
            event_type TEXT NOT NULL,
            request_path TEXT NOT NULL,
            accession_number TEXT,
            status_code INTEGER,
            success INTEGER NOT NULL,
            error_code TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def record_gateway_event(
    path: Path,
    *,
    event_type: str,
    request_path: str,
    accession_number: str | None = None,
    status_code: int | None = None,
    success: bool,
    error_code: str | None = None,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            _ensure_schema(connection)
            connection.execute(
                f"""
                INSERT INTO {AUDIT_TABLE} (
                    event_type,
                    request_path,
                    accession_number,
                    status_code,
                    success,
                    error_code,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    request_path,
                    accession_number,
                    status_code,
                    1 if success else 0,
                    error_code,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )
    except Exception as error:
        LOGGER.warning(
            "Gateway audit event write failed event_type=%s request_path=%s error=%s",
            event_type,
            request_path,
            error,
        )
