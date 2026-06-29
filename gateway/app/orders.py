from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo


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


def upsert_worklist_entry(worklist_payload: Any, entry: dict[str, Any]) -> dict[str, Any]:
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
            updated_entries.append(entry)
            replaced = True
        else:
            updated_entries.append(existing_entry)

    if not replaced:
        updated_entries.append(entry)

    return {"entries": updated_entries}
