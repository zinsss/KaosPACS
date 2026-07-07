from __future__ import annotations

import json
from datetime import datetime, time
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
REQUIRED_ORDER_FIELDS = (
    "ChartNo",
    "PatientName",
    "AccessionNumber",
    "StudyType",
    "Modality",
    "StationAET",
    "ScheduledAt",
    "Description",
)


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def validate_order_upsert(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["body must be an object"]

    errors = [
        f"{field} is required"
        for field in REQUIRED_ORDER_FIELDS
        if not text(payload.get(field))
    ]

    scheduled_at = text(payload.get("ScheduledAt"))
    if scheduled_at:
        try:
            parse_order_datetime(scheduled_at)
        except ValueError:
            errors.append("ScheduledAt must be an ISO datetime string")

    expires_at = text(payload.get("ExpiresAt"))
    if expires_at:
        try:
            parse_order_datetime(expires_at)
        except ValueError:
            errors.append("ExpiresAt must be an ISO datetime string")

    return errors


def validate_order_cancel(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["body must be an object"]
    if not text(payload.get("AccessionNumber")):
        return ["AccessionNumber is required"]
    return []


def parse_order_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SEOUL_TZ)
    return parsed.astimezone(SEOUL_TZ)


def default_expires_at(scheduled_at: datetime) -> str:
    end_of_day = datetime.combine(
        scheduled_at.astimezone(SEOUL_TZ).date(),
        time(23, 59, 59),
        tzinfo=SEOUL_TZ,
    )
    return end_of_day.isoformat()


def order_to_mwl_entry(payload: dict[str, Any]) -> dict[str, Any]:
    scheduled_at = parse_order_datetime(text(payload["ScheduledAt"]))
    expires_at = text(payload.get("ExpiresAt")) or default_expires_at(scheduled_at)
    accession_number = text(payload["AccessionNumber"])
    description = text(payload["Description"])

    return {
        "PatientID": text(payload["ChartNo"]),
        "PatientName": text(payload["PatientName"]),
        "PatientBirthDate": text(payload.get("PatientBirthDate")),
        "PatientSex": text(payload.get("PatientSex")) or "O",
        "AccessionNumber": accession_number,
        "Modality": text(payload["Modality"]),
        "ScheduledStationAETitle": text(payload["StationAET"]),
        "ScheduledProcedureStepDescription": description,
        "StudyDescription": description,
        "RequestedProcedureDescription": description,
        "RequestedProcedureID": accession_number,
        "ScheduledProcedureStepID": accession_number,
        "ScheduledProcedureStepStartDate": scheduled_at.strftime("%Y%m%d"),
        "ScheduledProcedureStepStartTime": scheduled_at.strftime("%H%M%S"),
        "Active": True,
        "ExpiresAt": expires_at,
        "StudyType": text(payload["StudyType"]),
        "SpecificCharacterSet": "ISO_IR 192",
    }


def upsert_worklist_entry(
    worklist_payload: Any,
    entry: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    existing_entries = []
    if isinstance(worklist_payload, dict) and isinstance(worklist_payload.get("entries"), list):
        existing_entries = worklist_payload["entries"]

    accession_number = entry["AccessionNumber"]
    updated_entries: list[Any] = []
    replaced = False
    for existing_entry in existing_entries:
        if (
            isinstance(existing_entry, dict)
            and text(existing_entry.get("AccessionNumber")) == accession_number
        ):
            if is_terminal_worklist_entry(existing_entry):
                updated_entries.append(existing_entry)
                replaced = True
                continue
            updated_entries.append(entry)
            replaced = True
        else:
            updated_entries.append(existing_entry)

    if not replaced:
        updated_entries.append(entry)
        return {"entries": updated_entries}, "upserted"

    if any(
        isinstance(existing_entry, dict)
        and text(existing_entry.get("AccessionNumber")) == accession_number
        and is_terminal_worklist_entry(existing_entry)
        for existing_entry in existing_entries
    ):
        return {"entries": updated_entries}, "ignored_terminal"
    return {"entries": updated_entries}, "upserted"


def is_terminal_worklist_entry(entry: dict[str, Any]) -> bool:
    return bool(
        text(entry.get("CompletedAt"))
        or text(entry.get("ExpiredAt"))
        or text(entry.get("CancelledAt"))
    )


def handle_order_post(handler: BaseHTTPRequestHandler, path: str) -> None:
    try:
        payload = read_json_request(handler)
    except json.JSONDecodeError as error:
        audit_event(
            handler,
            event_type="validation_error",
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

    if path == "/orders/upsert":
        handle_order_upsert(handler, path, payload)
        return
    handle_order_cancel(handler, path, payload)


def handle_order_upsert(handler: BaseHTTPRequestHandler, path: str, payload: Any) -> None:
    accession_number = text(payload.get("AccessionNumber")) if isinstance(payload, dict) else ""
    errors = validate_order_upsert(payload)
    if errors:
        audit_event(
            handler,
            event_type="validation_error",
            request_path=path,
            accession_number=accession_number or None,
            status_code=HTTPStatus.BAD_REQUEST,
            success=False,
            error_code="invalid_order",
        )
        json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid order", "details": errors},
        )
        return

    entry = order_to_mwl_entry(payload)
    client = mwl_client(handler)
    try:
        current_worklist = client.get_worklist()
        current_worklist_errors = validate_worklist_payload(current_worklist.payload)
        if current_worklist_errors:
            audit_event(
                handler,
                event_type="order_upsert",
                request_path=path,
                accession_number=accession_number,
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
        updated_worklist, action = upsert_worklist_entry(current_worklist.payload, entry)
        response = client.put_worklist(updated_worklist)
    except MwlHttpError as error:
        audit_event(
            handler,
            event_type="order_upsert",
            request_path=path,
            accession_number=accession_number,
            status_code=error.status_code,
            success=False,
            error_code="mwl_error",
        )
        proxy_mwl_response(handler, error.status_code, error.payload)
        return
    except MwlUnavailableError:
        audit_event(
            handler,
            event_type="order_upsert",
            request_path=path,
            accession_number=accession_number,
            status_code=HTTPStatus.BAD_GATEWAY,
            success=False,
            error_code="mwl_unavailable",
        )
        gateway_bad_gateway(handler)
        return

    audit_event(
        handler,
        event_type="order_upsert",
        request_path=path,
        accession_number=accession_number,
        status_code=response.status_code,
        success=response.status_code < 400,
    )
    json_response(
        handler,
        HTTPStatus.OK,
        {
            "status": "ok",
            "action": action,
            "AccessionNumber": accession_number,
        },
    )


def handle_order_cancel(handler: BaseHTTPRequestHandler, path: str, payload: Any) -> None:
    accession_number = text(payload.get("AccessionNumber")) if isinstance(payload, dict) else ""
    errors = validate_order_cancel(payload)
    if errors:
        audit_event(
            handler,
            event_type="validation_error",
            request_path=path,
            accession_number=accession_number or None,
            status_code=HTTPStatus.BAD_REQUEST,
            success=False,
            error_code="invalid_order_cancel",
        )
        json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid order cancel", "details": errors},
        )
        return

    try:
        response = mwl_client(handler).cancel_worklist(payload)
    except MwlHttpError as error:
        audit_event(
            handler,
            event_type="order_cancel",
            request_path=path,
            accession_number=accession_number,
            status_code=error.status_code,
            success=False,
            error_code="mwl_error",
        )
        proxy_mwl_response(handler, error.status_code, error.payload)
        return
    except MwlUnavailableError:
        audit_event(
            handler,
            event_type="order_cancel",
            request_path=path,
            accession_number=accession_number,
            status_code=HTTPStatus.BAD_GATEWAY,
            success=False,
            error_code="mwl_unavailable",
        )
        gateway_bad_gateway(handler)
        return

    audit_event(
        handler,
        event_type="order_cancel",
        request_path=path,
        accession_number=accession_number,
        status_code=response.status_code,
        success=response.status_code < 400,
    )
    proxy_mwl_response(handler, response.status_code, response.payload)
