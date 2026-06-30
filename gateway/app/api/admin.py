from __future__ import annotations

import json
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from zoneinfo import ZoneInfo

from app.api import (
    audit_event,
    gateway_bad_gateway,
    json_response,
    mwl_client,
    proxy_mwl_response,
    read_json_request,
)
from app.api.worklist import validate_worklist_payload
from app.clients.mwl import MwlHttpError, MwlUnavailableError


SEOUL_TZ = ZoneInfo("Asia/Seoul")
ALLOWED_STATUSES = {"completed", "cancelled", "expired"}
DEFAULT_STATUSES = ["completed", "cancelled", "expired"]


def validate_prune_request(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["body must be an object"]

    errors: list[str] = []
    if "dry_run" in payload and not isinstance(payload["dry_run"], bool):
        errors.append("dry_run must be a boolean")

    if "older_than_days" in payload:
        older_than_days = payload["older_than_days"]
        if (
            isinstance(older_than_days, bool)
            or not isinstance(older_than_days, int)
            or older_than_days < 0
        ):
            errors.append("older_than_days must be an integer >= 0")

    if "statuses" in payload:
        statuses = payload["statuses"]
        if not isinstance(statuses, list) or not statuses:
            errors.append("statuses must be a non-empty list")
        else:
            unknown_statuses = [
                status
                for status in statuses
                if not isinstance(status, str) or status not in ALLOWED_STATUSES
            ]
            if unknown_statuses:
                errors.append(
                    "statuses must only contain: completed, cancelled, expired"
                )

    return errors


def normalize_prune_request(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "dry_run": payload.get("dry_run", True),
        "older_than_days": payload.get("older_than_days", 7),
        "statuses": payload.get("statuses", DEFAULT_STATUSES),
    }


def prune_worklist(
    worklist_payload: dict[str, Any],
    *,
    dry_run: bool,
    older_than_days: int,
    statuses: list[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = _aware_datetime(now or datetime.now(tz=SEOUL_TZ))
    cutoff = current_time - timedelta(days=older_than_days)
    entries = worklist_payload["entries"]

    kept_entries = []
    removed_entries = []
    for entry in entries:
        prune_match = _prune_match(entry, set(statuses), cutoff, current_time)
        if prune_match is None:
            kept_entries.append(entry)
            continue

        removed_entries.append(prune_match)

    return {
        "worklist": {"entries": kept_entries},
        "summary": {
            "status": "ok",
            "dry_run": dry_run,
            "older_than_days": older_than_days,
            "statuses": statuses,
            "before_count": len(entries),
            "after_count": len(kept_entries),
            "removed_count": len(removed_entries),
            "removed": removed_entries,
        },
    }


def _prune_match(
    entry: Any,
    statuses: set[str],
    cutoff: datetime,
    now: datetime,
) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    if entry.get("Active") is not False:
        return None

    for status, timestamp_field in (
        ("completed", "CompletedAt"),
        ("cancelled", "CancelledAt"),
        ("expired", "ExpiredAt"),
    ):
        if status not in statuses:
            continue
        timestamp = _parse_datetime(entry.get(timestamp_field))
        if timestamp is None:
            continue
        if status == "expired" and timestamp >= now:
            continue
        if timestamp > cutoff:
            continue
        return {
            "AccessionNumber": str(entry.get("AccessionNumber", "")),
            "reason": status,
            "timestamp": timestamp.isoformat(),
        }

    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_datetime(parsed)


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SEOUL_TZ)
    return value.astimezone(SEOUL_TZ)


def handle_admin_worklist_prune(handler: BaseHTTPRequestHandler, path: str) -> None:
    try:
        payload = read_json_request(handler)
    except json.JSONDecodeError as error:
        audit_event(
            handler,
            event_type="admin_worklist_prune",
            request_path=path,
            status_code=HTTPStatus.BAD_REQUEST,
            success=False,
            error_code="invalid_json",
        )
        json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid JSON", "details": [str(error)]},
        )
        return

    errors = validate_prune_request(payload)
    if errors:
        audit_event(
            handler,
            event_type="admin_worklist_prune",
            request_path=path,
            status_code=HTTPStatus.BAD_REQUEST,
            success=False,
            error_code="invalid_request",
        )
        json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid prune request", "details": errors},
        )
        return

    options = normalize_prune_request(payload)
    client = mwl_client(handler)
    try:
        current_worklist = client.get_worklist()
        current_worklist_errors = validate_worklist_payload(current_worklist.payload)
        if current_worklist_errors:
            audit_event(
                handler,
                event_type="admin_worklist_prune",
                request_path=path,
                status_code=HTTPStatus.BAD_GATEWAY,
                success=False,
                error_code="invalid_mwl_worklist",
            )
            json_response(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "bad_gateway",
                    "message": "MWL API returned an invalid worklist",
                },
            )
            return

        prune_result = prune_worklist(current_worklist.payload, **options)
        if not options["dry_run"]:
            client.put_worklist(prune_result["worklist"])
    except MwlHttpError as error:
        audit_event(
            handler,
            event_type="admin_worklist_prune",
            request_path=path,
            status_code=error.status_code,
            success=False,
            error_code="mwl_error",
        )
        proxy_mwl_response(handler, error.status_code, error.payload)
        return
    except MwlUnavailableError:
        audit_event(
            handler,
            event_type="admin_worklist_prune",
            request_path=path,
            status_code=HTTPStatus.BAD_GATEWAY,
            success=False,
            error_code="mwl_unavailable",
        )
        gateway_bad_gateway(handler)
        return

    audit_event(
        handler,
        event_type="admin_worklist_prune",
        request_path=path,
        status_code=HTTPStatus.OK,
        success=True,
    )
    json_response(handler, HTTPStatus.OK, prune_result["summary"])
