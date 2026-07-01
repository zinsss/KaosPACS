from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from app.api import audit_event, gateway_bad_gateway, json_response, mwl_client, text
from app.clients.mwl import MwlHttpError, MwlUnavailableError


COUNT_STATES = ("active", "completed", "expired", "cancelled", "inactive")


def imaging_worklist_payload(worklist_payload: Any) -> dict[str, Any]:
    entries = worklist_payload.get("entries", []) if isinstance(worklist_payload, dict) else []
    if not isinstance(entries, list):
        entries = []

    mapped_entries = [
        _imaging_entry(entry)
        for entry in entries
        if isinstance(entry, dict)
    ]
    mapped_entries.sort(key=_sort_key)

    counts = {state: 0 for state in COUNT_STATES}
    for entry in mapped_entries:
        counts[entry["state"]] += 1

    return {
        "entries": mapped_entries,
        "counts": counts,
    }


def handle_get_imaging_worklist(handler: BaseHTTPRequestHandler, path: str) -> None:
    try:
        response = mwl_client(handler).get_worklist()
    except MwlHttpError as error:
        audit_event(
            handler,
            event_type="imaging_worklist_get",
            request_path=path,
            status_code=error.status_code,
            success=False,
            error_code="mwl_error",
        )
        json_response(
            handler,
            HTTPStatus.BAD_GATEWAY,
            {
                "error": "bad_gateway",
                "message": "MWL API returned an error",
            },
        )
        return
    except MwlUnavailableError:
        audit_event(
            handler,
            event_type="mwl_unavailable",
            request_path=path,
            status_code=HTTPStatus.BAD_GATEWAY,
            success=False,
            error_code="mwl_unavailable",
        )
        gateway_bad_gateway(handler)
        return

    payload = imaging_worklist_payload(response.payload)
    audit_event(
        handler,
        event_type="imaging_worklist_get",
        request_path=path,
        status_code=HTTPStatus.OK,
        success=True,
    )
    json_response(handler, HTTPStatus.OK, payload)


def _imaging_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": _state(entry),
        "AccessionNumber": text(entry.get("AccessionNumber")),
        "PatientID": text(entry.get("PatientID")),
        "PatientName": text(entry.get("PatientName")),
        "PatientBirthDate": text(entry.get("PatientBirthDate")),
        "PatientSex": text(entry.get("PatientSex")),
        "Modality": text(entry.get("Modality")),
        "ScheduledStationAETitle": text(entry.get("ScheduledStationAETitle")),
        "ScheduledAt": _scheduled_at(entry),
        "Description": _description(entry),
        "CompletedAt": _nullable_text(entry.get("CompletedAt")),
        "ExpiredAt": _nullable_text(entry.get("ExpiredAt")),
        "ExpireReason": _nullable_text(entry.get("ExpireReason")),
        "CancelledAt": _nullable_text(entry.get("CancelledAt")),
        "CancelReason": _nullable_text(entry.get("CancelReason")),
    }


def _state(entry: dict[str, Any]) -> str:
    if text(entry.get("CancelledAt")):
        return "cancelled"
    if text(entry.get("CompletedAt")):
        return "completed"
    if text(entry.get("ExpiredAt")):
        return "expired"
    if entry.get("Active") is True:
        return "active"
    return "inactive"


def _scheduled_at(entry: dict[str, Any]) -> str:
    date = text(entry.get("ScheduledProcedureStepStartDate"))
    step_time = text(entry.get("ScheduledProcedureStepStartTime"))
    if not date:
        return ""
    if len(date) == 8 and date.isdigit():
        date = f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
    if not step_time:
        return date
    if len(step_time) >= 6:
        step_time = f"{step_time[0:2]}:{step_time[2:4]}:{step_time[4:6]}"
    return f"{date}T{step_time}"


def _description(entry: dict[str, Any]) -> str:
    return (
        text(entry.get("ScheduledProcedureStepDescription"))
        or text(entry.get("StudyDescription"))
        or text(entry.get("RequestedProcedureDescription"))
    )


def _nullable_text(value: Any) -> str | None:
    value_text = text(value)
    return value_text or None


def _sort_key(entry: dict[str, Any]) -> tuple[int, float, str]:
    state = entry["state"]
    accession = entry["AccessionNumber"]
    if state == "active":
        return (0, _timestamp(entry["ScheduledAt"], future=True), accession)
    if state in {"completed", "expired", "cancelled"}:
        return (1, -_timestamp(_terminal_timestamp(entry)), accession)
    return (2, 0.0, accession)


def _terminal_timestamp(entry: dict[str, Any]) -> str:
    return (
        entry["CompletedAt"]
        or entry["ExpiredAt"]
        or entry["CancelledAt"]
        or ""
    )


def _timestamp(value: str | None, *, future: bool = False) -> float:
    raw = text(value)
    if not raw:
        return float("inf") if future else 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("inf") if future else 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
