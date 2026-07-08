from app.api.orders import (
    default_expires_at,
    order_to_mwl_entry,
    parse_order_datetime,
    upsert_worklist_entry,
    validate_order_upsert,
)


def valid_order_payload(**overrides):
    payload = {
        "ChartNo": "12345",
        "PatientName": "TEST^PATIENT",
        "PatientBirthDate": "19700101",
        "PatientSex": "O",
        "AccessionNumber": "20260629-12345-1",
        "StudyType": "BMD",
        "Modality": "BMD",
        "StationAET": "BMD",
        "ScheduledAt": "2026-06-29T09:00:00+09:00",
        "Description": "BMD",
        "ExpiresAt": "2026-06-29T23:59:59+09:00",
    }
    payload.update(overrides)
    return payload


def test_order_to_mwl_entry_maps_dicom_fields() -> None:
    entry = order_to_mwl_entry(valid_order_payload())

    assert entry["PatientID"] == "12345"
    assert entry["PatientName"] == "TEST^PATIENT"
    assert entry["PatientBirthDate"] == "19700101"
    assert entry["PatientSex"] == "O"
    assert entry["AccessionNumber"] == "20260629-12345-1"
    assert entry["Modality"] == "BMD"
    assert entry["ScheduledStationAETitle"] == "BMD"
    assert entry["ScheduledProcedureStepDescription"] == "BMD"
    assert entry["StudyDescription"] == "BMD"
    assert entry["RequestedProcedureDescription"] == "BMD"
    assert entry["RequestedProcedureID"] == "20260629-12345-1"
    assert entry["ScheduledProcedureStepID"] == "20260629-12345-1"
    assert entry["ScheduledProcedureStepStartDate"] == "20260629"
    assert entry["ScheduledProcedureStepStartTime"] == "090000"
    assert entry["Active"] is True
    assert entry["ExpiresAt"] == "2026-06-29T23:59:59+09:00"
    assert entry["StudyType"] == "BMD"
    assert entry["SpecificCharacterSet"] == "ISO_IR 192"


def test_order_to_mwl_entry_defaults_optional_fields_and_expires_at() -> None:
    entry = order_to_mwl_entry(
        valid_order_payload(
            PatientBirthDate=None,
            PatientSex="",
            ExpiresAt="",
            ScheduledAt="2026-06-29T01:00:00Z",
        )
    )

    assert entry["PatientBirthDate"] == ""
    assert entry["PatientSex"] == "O"
    assert entry["ScheduledProcedureStepStartDate"] == "20260629"
    assert entry["ScheduledProcedureStepStartTime"] == "100000"
    assert entry["ExpiresAt"] == "2026-06-29T23:59:59+09:00"


def test_default_expires_at_is_end_of_scheduled_date_in_seoul() -> None:
    scheduled_at = parse_order_datetime("2026-06-28T18:00:00Z")

    assert default_expires_at(scheduled_at) == "2026-06-29T23:59:59+09:00"


def test_validate_order_upsert_reports_missing_fields() -> None:
    errors = validate_order_upsert({"ChartNo": "12345"})

    assert "PatientName is required" in errors
    assert "AccessionNumber is required" in errors
    assert "ScheduledAt is required" in errors


def test_upsert_worklist_entry_replaces_matching_accession() -> None:
    replacement = {"AccessionNumber": "A1", "PatientID": "new"}
    result = upsert_worklist_entry(
        {
            "entries": [
                {"AccessionNumber": "A1", "PatientID": "old"},
                {"AccessionNumber": "A2", "PatientID": "keep"},
            ]
        },
        replacement,
    )

    assert result == {
        "entries": [
            replacement,
            {"AccessionNumber": "A2", "PatientID": "keep"},
        ]
    }


def test_upsert_worklist_entry_preserves_completed_terminal_state() -> None:
    replacement = {
        "AccessionNumber": "A1",
        "PatientID": "new",
        "PatientName": "New Name",
        "Active": True,
    }
    result = upsert_worklist_entry(
        {
            "entries": [
                {
                    "AccessionNumber": "A1",
                    "PatientID": "old",
                    "Active": False,
                    "CompletedAt": "2026-07-08T09:56:11+09:00",
                }
            ]
        },
        replacement,
    )

    assert result["entries"][0]["PatientID"] == "new"
    assert result["entries"][0]["PatientName"] == "New Name"
    assert result["entries"][0]["Active"] is False
    assert result["entries"][0]["CompletedAt"] == "2026-07-08T09:56:11+09:00"


def test_upsert_worklist_entry_preserves_cancelled_terminal_state() -> None:
    replacement = {"AccessionNumber": "A1", "PatientID": "new", "Active": True}
    result = upsert_worklist_entry(
        {
            "entries": [
                {
                    "AccessionNumber": "A1",
                    "PatientID": "old",
                    "Active": False,
                    "CancelledAt": "2026-07-08T10:00:00+09:00",
                    "CancelReason": "cancelled_in_source",
                }
            ]
        },
        replacement,
    )

    assert result["entries"][0]["Active"] is False
    assert result["entries"][0]["CancelledAt"] == "2026-07-08T10:00:00+09:00"
    assert result["entries"][0]["CancelReason"] == "cancelled_in_source"


def test_upsert_worklist_entry_appends_when_not_found() -> None:
    new_entry = {"AccessionNumber": "A2", "PatientID": "new"}
    result = upsert_worklist_entry(
        {"entries": [{"AccessionNumber": "A1", "PatientID": "keep"}]},
        new_entry,
    )

    assert result == {
        "entries": [
            {"AccessionNumber": "A1", "PatientID": "keep"},
            new_entry,
        ]
    }
