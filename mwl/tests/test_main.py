import json
import sqlite3
import sys
from io import BytesIO
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.filereader import dcmread
from pydicom.filewriter import dcmwrite
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import (
    EXPIRE_REASON_WITHOUT_IMAGING,
    expire_stale_entries,
    expire_worklist_file,
    initialize_worklist_from_seed,
    load_worklist_datasets,
    matches_query,
    start_api_server,
)


NOW = datetime(2026, 6, 27, 9, 0, 0, tzinfo=timezone.utc)
NOW_SEOUL = "2026-06-27T18:00:00+09:00"


def write_worklist(tmp_path, entries):
    path = tmp_path / "worklist.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def api_request(server, method, path, payload=None):
    host, port = server.server_address
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    connection = HTTPConnection(host, port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        parsed = json.loads(raw.decode("utf-8")) if raw else None
        return response.status, parsed
    finally:
        connection.close()


def start_test_api(path, audit_db_path=None):
    audit_db_path = audit_db_path or path.with_name("mwl_audit.sqlite3")
    return start_api_server("127.0.0.1", 0, path, audit_db_path)


def audit_rows(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM mwl_audit ORDER BY accession_number"
            )
        ]


def audit_columns(path):
    with sqlite3.connect(path) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(mwl_audit)")]


def audit_event_rows(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM mwl_events ORDER BY id"
            )
        ]


def valid_entry(**overrides):
    entry = {
        "Active": True,
        "ExpiresAt": "2099-12-31T23:59:59+09:00",
        "SpecificCharacterSet": "ISO_IR 192",
        "PatientName": "TEST^VALID",
        "PatientID": "VALID001",
        "PatientBirthDate": "19700101",
        "PatientSex": "O",
        "AccessionNumber": "VALID001",
        "Modality": "BMD",
        "ScheduledStationAETitle": "BMD",
        "ScheduledProcedureStepDescription": "BMD TEST",
        "StudyDescription": "BMD TEST",
        "RequestedProcedureDescription": "BMD TEST",
        "RequestedProcedureID": "VALID001",
        "ScheduledProcedureStepID": "VALID001",
        "ScheduledProcedureStepStartDate": "20260627",
        "ScheduledProcedureStepStartTime": "090000",
    }
    entry.update(overrides)
    return entry


def test_load_worklist_maps_hardcoded_bmd_item():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 2
    dataset = datasets[0]
    assert dataset.PatientName == "TEST^BMD"
    assert dataset.PatientID == "KAOSMWL001"
    assert dataset.AccessionNumber == "KAOSMWL001"
    assert dataset.ScheduledProcedureStepSequence[0].Modality == "BMD"
    assert dataset.ScheduledProcedureStepSequence[0].ScheduledStationAETitle == "BMD"


def test_matches_query_by_patient_and_station():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    item = load_worklist_datasets(path, now=NOW)[0]

    query = Dataset()
    query.PatientID = "KAOSMWL001"
    step = Dataset()
    step.ScheduledStationAETitle = "BMD"
    query.ScheduledProcedureStepSequence = Sequence([step])

    assert matches_query(query, item)


def test_filters_multiple_entries_by_modality_and_station():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    items = load_worklist_datasets(path, now=NOW)

    query = Dataset()
    step = Dataset()
    step.Modality = "CR"
    step.ScheduledStationAETitle = "INNOVISION"
    query.ScheduledProcedureStepSequence = Sequence([step])

    matches = [item for item in items if matches_query(query, item)]

    assert len(matches) == 1
    assert matches[0].PatientID == "KAOSMWL002"


def test_filters_by_accession_number():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    items = load_worklist_datasets(path, now=NOW)

    query = Dataset()
    query.AccessionNumber = "KAOSMWL002"

    matches = [item for item in items if matches_query(query, item)]

    assert len(matches) == 1
    assert matches[0].PatientName == "TEST^XRAY"


def test_non_matching_accession_is_rejected():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    item = load_worklist_datasets(path, now=NOW)[0]

    query = Dataset()
    query.AccessionNumber = "OTHER"

    assert not matches_query(query, item)


def test_valid_entry_is_returned(tmp_path):
    path = write_worklist(tmp_path, [valid_entry()])

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 1
    assert datasets[0].PatientID == "VALID001"


def test_mwl_dicom_character_set_defaults_to_legacy_korean(tmp_path):
    entry = valid_entry()
    entry.pop("SpecificCharacterSet")
    path = write_worklist(tmp_path, [entry])

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 1
    assert datasets[0].SpecificCharacterSet == "ISO 2022 IR 149"


def test_mwl_dicom_character_set_overrides_json_entry(tmp_path):
    path = write_worklist(
        tmp_path,
        [valid_entry(SpecificCharacterSet="ISO_IR 192")],
    )

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 1
    assert datasets[0].SpecificCharacterSet == "ISO 2022 IR 149"


def test_mwl_dicom_character_set_can_be_configured_to_utf8(tmp_path):
    path = write_worklist(tmp_path, [valid_entry(SpecificCharacterSet="ISO 2022 IR 149")])

    datasets = load_worklist_datasets(
        path,
        now=NOW,
        dicom_character_set="ISO_IR 192",
    )

    assert len(datasets) == 1
    assert datasets[0].SpecificCharacterSet == "ISO_IR 192"


def test_korean_text_is_preserved_in_mwl_dataset(tmp_path):
    path = write_worklist(
        tmp_path,
        [
            valid_entry(
                PatientName="홍길동",
                PatientID="PT-KR-001",
                AccessionNumber="ACC-KR-001",
                StudyDescription="골밀도 검사",
                RequestedProcedureDescription="골밀도 검사",
                ScheduledProcedureStepDescription="골밀도 검사",
            )
        ],
    )

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 1
    assert str(datasets[0].SpecificCharacterSet) == "ISO 2022 IR 149"
    assert str(datasets[0].PatientName) == "홍길동"
    assert (
        str(datasets[0].ScheduledProcedureStepSequence[0].ScheduledProcedureStepDescription)
        == "골밀도 검사"
    )


def test_korean_mwl_dataset_encodes_as_euc_kr_for_legacy_charset(tmp_path):
    path = write_worklist(
        tmp_path,
        [
            valid_entry(
                PatientName="이진성",
                PatientID="7435",
                AccessionNumber="11",
                StudyDescription="골밀도 검사",
                RequestedProcedureDescription="골밀도 검사",
                ScheduledProcedureStepDescription="골밀도검사",
            )
        ],
    )

    dataset = load_worklist_datasets(path, now=NOW)[0]
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.31"
    dataset.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    dataset.SOPClassUID = dataset.file_meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = dataset.file_meta.MediaStorageSOPInstanceUID

    buffer = BytesIO()
    dcmwrite(buffer, dataset, write_like_original=False)
    raw = buffer.getvalue()

    assert "이진성".encode("euc_kr") in raw
    assert "이진성".encode("utf-8") not in raw

    buffer.seek(0)
    reread = dcmread(buffer)
    assert reread.SpecificCharacterSet == "ISO 2022 IR 149"
    assert str(reread.PatientName) == "이진성"
    assert (
        str(reread.ScheduledProcedureStepSequence[0].ScheduledProcedureStepDescription)
        == "골밀도검사"
    )


def test_inactive_entry_is_not_returned(tmp_path):
    path = write_worklist(tmp_path, [valid_entry(Active=False)])

    datasets = load_worklist_datasets(path, now=NOW)

    assert datasets == []


def test_expired_entry_is_not_returned(tmp_path):
    path = write_worklist(tmp_path, [valid_entry(ExpiresAt="2000-01-01T00:00:00+00:00")])

    datasets = load_worklist_datasets(path, now=NOW)

    assert datasets == []
    entry = json.loads(path.read_text(encoding="utf-8"))["entries"][0]
    assert entry["Active"] is False
    assert entry["ExpiredAt"] == NOW_SEOUL
    assert entry["ExpireReason"] == EXPIRE_REASON_WITHOUT_IMAGING


def test_active_entry_with_past_expires_at_becomes_expired(tmp_path):
    payload = {
        "entries": [
            valid_entry(ExpiresAt="2026-06-26T23:59:59+00:00"),
        ]
    }

    expired = expire_stale_entries(payload, now=NOW)

    assert len(expired) == 1
    entry = payload["entries"][0]
    assert entry["Active"] is False
    assert entry["ExpiredAt"] == NOW_SEOUL
    assert entry["ExpireReason"] == EXPIRE_REASON_WITHOUT_IMAGING
    assert "CompletedAt" not in entry
    assert "CancelledAt" not in entry
    assert "CancelReason" not in entry


def test_active_entry_with_past_scheduled_date_without_expires_at_becomes_expired(tmp_path):
    payload = {
        "entries": [
            valid_entry(
                ExpiresAt="",
                ScheduledProcedureStepStartDate="20260626",
                ScheduledProcedureStepStartTime="090000",
            ),
        ]
    }

    expired = expire_stale_entries(payload, now=NOW)

    assert len(expired) == 1
    assert payload["entries"][0]["Active"] is False
    assert payload["entries"][0]["ExpireReason"] == EXPIRE_REASON_WITHOUT_IMAGING


def test_future_active_entry_remains_active():
    payload = {
        "entries": [
            valid_entry(ExpiresAt="2026-06-28T23:59:59+00:00"),
        ]
    }

    expired = expire_stale_entries(payload, now=NOW)

    assert expired == []
    assert payload["entries"][0]["Active"] is True
    assert "ExpiredAt" not in payload["entries"][0]


def test_completed_entry_never_becomes_expired():
    payload = {
        "entries": [
            valid_entry(
                ExpiresAt="2026-06-26T23:59:59+00:00",
                CompletedAt="2026-06-27T08:00:00+00:00",
            ),
        ]
    }

    expired = expire_stale_entries(payload, now=NOW)

    assert expired == []
    assert payload["entries"][0]["Active"] is True
    assert "ExpiredAt" not in payload["entries"][0]


def test_cancelled_entry_never_becomes_expired():
    payload = {
        "entries": [
            valid_entry(
                ExpiresAt="2026-06-26T23:59:59+00:00",
                CancelledAt="2026-06-27T08:00:00+00:00",
            ),
        ]
    }

    expired = expire_stale_entries(payload, now=NOW)

    assert expired == []
    assert payload["entries"][0]["Active"] is True
    assert "ExpiredAt" not in payload["entries"][0]


def test_invalid_entry_missing_required_field_is_skipped(tmp_path, caplog):
    path = write_worklist(
        tmp_path,
        [
            valid_entry(PatientID=""),
            valid_entry(PatientID="VALID002", AccessionNumber="VALID002"),
        ],
    )

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 1
    assert datasets[0].PatientID == "VALID002"
    assert "missing required fields" in caplog.text
    assert "PatientID" in caplog.text


def test_invalid_expires_at_is_skipped(tmp_path, caplog):
    path = write_worklist(tmp_path, [valid_entry(ExpiresAt="not-a-date")])

    datasets = load_worklist_datasets(path, now=NOW)

    assert datasets == []
    assert "invalid ExpiresAt" in caplog.text


def test_api_get_worklist_returns_current_entries(tmp_path):
    path = write_worklist(tmp_path, [valid_entry()])
    server = start_test_api(path)
    try:
        status, payload = api_request(server, "GET", "/worklist")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert payload["entries"][0]["PatientID"] == "VALID001"


def test_api_get_worklist_expires_stale_entries_and_keeps_inactive_visible(tmp_path):
    path = write_worklist(
        tmp_path,
        [valid_entry(ExpiresAt="2026-06-26T23:59:59+00:00")],
    )
    audit_db = tmp_path / "audit.sqlite3"
    server = start_test_api(path, audit_db)
    try:
        status, payload = api_request(server, "GET", "/worklist")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    entry = payload["entries"][0]
    assert entry["Active"] is False
    assert entry["ExpiredAt"]
    assert entry["ExpireReason"] == EXPIRE_REASON_WITHOUT_IMAGING
    assert load_worklist_datasets(path, now=NOW) == []
    assert audit_event_rows(audit_db)[0]["event_type"] == "worklist_expired"
    rows = audit_rows(audit_db)
    assert rows[0]["accession_number"] == "VALID001"
    assert rows[0]["status"] == "expired"
    assert rows[0]["chart_no"] is None


def test_initialize_worklist_from_seed_copies_only_when_missing(tmp_path):
    seed = write_worklist(tmp_path, [valid_entry(PatientID="SEED", AccessionNumber="SEED")])
    runtime = tmp_path / "data" / "worklist.json"

    initialize_worklist_from_seed(runtime, seed)

    assert json.loads(runtime.read_text(encoding="utf-8"))["entries"][0]["PatientID"] == "SEED"

    replacement = {"entries": [valid_entry(PatientID="RUNTIME", AccessionNumber="RUNTIME")]}
    runtime.write_text(json.dumps(replacement), encoding="utf-8")

    initialize_worklist_from_seed(runtime, seed)

    assert json.loads(runtime.read_text(encoding="utf-8")) == replacement


def test_api_valid_put_updates_file(tmp_path):
    path = write_worklist(tmp_path, [valid_entry(PatientID="OLD")])
    audit_db = tmp_path / "audit.sqlite3"
    server = start_test_api(path, audit_db)
    replacement = {
        "entries": [
            valid_entry(PatientID="NEW001", AccessionNumber="NEW001"),
        ]
    }
    try:
        status, payload = api_request(server, "PUT", "/worklist", replacement)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert payload["entries"][0]["PatientID"] == "NEW001"
    assert json.loads(path.read_text(encoding="utf-8"))["entries"][0]["PatientID"] == "NEW001"
    rows = audit_rows(audit_db)
    assert rows[0]["accession_number"] == "NEW001"
    assert rows[0]["chart_no"] == "NEW001"
    assert rows[0]["study_type"] == "BMD TEST"
    assert rows[0]["modality"] == "BMD"
    assert rows[0]["station_aet"] == "BMD"
    assert rows[0]["scheduled_at"] == "2026-06-27T09:00:00"
    assert rows[0]["status"] == "active"


def test_api_invalid_put_does_not_overwrite_file(tmp_path):
    original = {"entries": [valid_entry(PatientID="ORIGINAL")]}
    path = write_worklist(tmp_path, original["entries"])
    server = start_test_api(path)
    invalid = {"entries": [valid_entry(PatientID="")]}
    try:
        status, payload = api_request(server, "PUT", "/worklist", invalid)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 400
    assert "PatientID" in " ".join(payload["details"])
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_api_put_accepts_inactive_and_expired_but_loader_filters_them(tmp_path):
    path = write_worklist(tmp_path, [valid_entry()])
    server = start_test_api(path)
    replacement = {
        "entries": [
            valid_entry(PatientID="INACTIVE", AccessionNumber="INACTIVE", Active=False),
            valid_entry(
                PatientID="EXPIRED",
                AccessionNumber="EXPIRED",
                ExpiresAt="2000-01-01T00:00:00+00:00",
            ),
        ]
    }
    try:
        status, payload = api_request(server, "PUT", "/worklist", replacement)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert len(payload["entries"]) == 2
    assert load_worklist_datasets(path, now=NOW) == []


def test_api_complete_marks_entry_inactive_without_deleting(tmp_path):
    path = write_worklist(tmp_path, [valid_entry()])
    audit_db = tmp_path / "audit.sqlite3"
    server = start_test_api(path, audit_db)
    api_request(server, "PUT", "/worklist", {"entries": [valid_entry()]})
    try:
        status, payload = api_request(
            server,
            "POST",
            "/worklist/complete",
            {"AccessionNumber": "VALID001"},
        )
    finally:
        server.shutdown()
        server.server_close()

    entry = payload["worklist"]["entries"][0]
    assert status == 200
    assert payload["updated"] == 1
    assert entry["Active"] is False
    assert entry["PatientName"] == "TEST^VALID"
    assert "CompletedAt" in entry
    assert load_worklist_datasets(path, now=NOW) == []
    rows = audit_rows(audit_db)
    assert rows[0]["status"] == "completed"
    assert rows[0]["completed_at"]


def test_api_cancel_marks_entry_inactive_with_reason(tmp_path):
    path = write_worklist(tmp_path, [valid_entry()])
    audit_db = tmp_path / "audit.sqlite3"
    server = start_test_api(path, audit_db)
    api_request(server, "PUT", "/worklist", {"entries": [valid_entry()]})
    try:
        status, payload = api_request(
            server,
            "POST",
            "/worklist/cancel",
            {"AccessionNumber": "VALID001", "CancelReason": "patient no-show"},
        )
    finally:
        server.shutdown()
        server.server_close()

    entry = payload["worklist"]["entries"][0]
    assert status == 200
    assert entry["Active"] is False
    assert entry["CancelReason"] == "patient no-show"
    assert "CancelledAt" in entry
    assert load_worklist_datasets(path, now=NOW) == []
    rows = audit_rows(audit_db)
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["cancelled_at"]
    assert rows[0]["cancel_reason"] == "patient no-show"


def test_explicit_cancel_remains_cancelled_not_expired(tmp_path):
    path = write_worklist(
        tmp_path,
        [valid_entry(ExpiresAt="2026-06-26T23:59:59+00:00")],
    )
    audit_db = tmp_path / "audit.sqlite3"
    server = start_test_api(path, audit_db)
    try:
        status, payload = api_request(
            server,
            "POST",
            "/worklist/cancel",
            {"AccessionNumber": "VALID001", "CancelReason": "deleted_in_source"},
        )
    finally:
        server.shutdown()
        server.server_close()

    entry = payload["worklist"]["entries"][0]
    assert status == 200
    assert entry["Active"] is False
    assert entry["CancelReason"] == "deleted_in_source"
    assert "CancelledAt" in entry
    assert "ExpiredAt" not in entry


def test_source_cancel_after_expiry_sets_cancelled_and_keeps_expired_trace(tmp_path):
    path = write_worklist(
        tmp_path,
        [valid_entry(ExpiresAt="2026-06-26T23:59:59+00:00")],
    )
    audit_db = tmp_path / "audit.sqlite3"
    expire_worklist_file(path, audit_db_path=audit_db, now=NOW)
    server = start_test_api(path, audit_db)
    try:
        status, payload = api_request(
            server,
            "POST",
            "/worklist/cancel",
            {"AccessionNumber": "VALID001", "CancelReason": "cancelled_in_source"},
        )
    finally:
        server.shutdown()
        server.server_close()

    entry = payload["worklist"]["entries"][0]
    assert status == 200
    assert entry["CancelledAt"]
    assert entry["CancelReason"] == "cancelled_in_source"
    assert entry["ExpiredAt"]
    assert entry["ExpireReason"] == EXPIRE_REASON_WITHOUT_IMAGING
    rows = audit_rows(audit_db)
    assert rows[0]["status"] == "cancelled"


def test_audit_db_does_not_contain_demographic_columns(tmp_path):
    path = write_worklist(tmp_path, [valid_entry()])
    audit_db = tmp_path / "audit.sqlite3"
    server = start_test_api(path, audit_db)
    try:
        status, _ = api_request(server, "PUT", "/worklist", {"entries": [valid_entry()]})
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    columns = set(audit_columns(audit_db))
    assert "PatientName" not in columns
    assert "PatientBirthDate" not in columns
    assert "PatientSex" not in columns
    assert "patient_name" not in columns
    assert "patient_birth_date" not in columns
    assert "patient_sex" not in columns


def test_expiry_audit_event_does_not_contain_demographic_columns(tmp_path):
    path = write_worklist(
        tmp_path,
        [
            valid_entry(
                PatientName="PRIVATE^NAME",
                PatientID="PRIVATE-ID",
                PatientBirthDate="19770202",
                PatientSex="F",
                ExpiresAt="2026-06-26T23:59:59+00:00",
            )
        ],
    )
    audit_db = tmp_path / "audit.sqlite3"

    expire_worklist_file(path, audit_db_path=audit_db, now=NOW)

    event_columns = {
        row[1]
        for row in sqlite3.connect(audit_db).execute("PRAGMA table_info(mwl_events)")
    }
    assert event_columns == {
        "id",
        "event_type",
        "accession_number",
        "success",
        "created_at",
    }
    serialized_events = json.dumps(audit_event_rows(audit_db))
    assert "PRIVATE^NAME" not in serialized_events
    assert "PRIVATE-ID" not in serialized_events
    assert "19770202" not in serialized_events
    assert "PatientName" not in serialized_events
    assert "PatientID" not in serialized_events
