import json
import logging
import socket
import sqlite3
from io import BytesIO
from pathlib import Path

from pydicom import dcmread
from pydicom.filewriter import dcmwrite
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from pynetdicom import AE

from app.config import GatewayConfig
from app.dicom import server as dicom_server
from app.dicom.forwarder import DicomForwarder, ForwardResult
from app.dicom.queue import QUEUE_TABLE, get_queue_counts
from app.dicom.server import (
    WRITE_FAILURE_STATUS,
    GatewayDicomServer,
    handle_store,
    start_dicom_listener,
)
from app.dicom.storage import safe_dicom_filename, store_dataset
from app.services.audit import init_audit_db
from app.services.operational_metadata import get_by_accession_number


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _minimal_dataset() -> Dataset:
    sop_instance_uid = generate_uid()
    study_instance_uid = generate_uid()
    series_instance_uid = generate_uid()
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    dataset.file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.StudyInstanceUID = study_instance_uid
    dataset.SeriesInstanceUID = series_instance_uid
    dataset.Modality = "OT"
    dataset.AccessionNumber = "ACC-TEST"
    dataset.PatientName = "SHOULD^NOTLOG"
    dataset.PatientID = "SECRETID"
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False
    return dataset


def test_disabled_listener_does_not_start(caplog) -> None:
    caplog.set_level(logging.INFO)
    config = GatewayConfig(gateway_dicom_enabled=False)

    server = start_dicom_listener(config)

    assert server is None
    assert "disabled" in caplog.text


def test_safe_dicom_filename_uses_sop_instance_uid() -> None:
    dataset = Dataset()
    dataset.SOPInstanceUID = "1.2.840.113619.2.55.3"

    assert safe_dicom_filename(dataset) == "1.2.840.113619.2.55.3.dcm"


def test_safe_dicom_filename_sanitizes_unexpected_values() -> None:
    dataset = type("UnexpectedDataset", (), {"SOPInstanceUID": "../bad/value"})()

    assert safe_dicom_filename(dataset) == "bad_value.dcm"


def test_handle_store_failure_returns_status_without_phi_logs(tmp_path, caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()

    def fail_store(_dataset, _storage_dir):
        raise RuntimeError("synthetic save failure with SHOULD^NOTLOG SECRETID")

    monkeypatch.setattr(dicom_server, "store_dataset", fail_store)

    status = handle_store(event, tmp_path)

    assert status == WRITE_FAILURE_STATUS
    assert str(dataset.SOPInstanceUID) in caplog.text
    assert "RuntimeError" in caplog.text
    assert "synthetic save failure" not in caplog.text
    assert "ACC-TEST" not in caplog.text
    assert "OT" not in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


class RecordingForwarder:
    def __init__(self, result: ForwardResult) -> None:
        self.result = result
        self.paths: list[Path] = []

    def forward_file(self, path: Path) -> ForwardResult:
        self.paths.append(path)
        return self.result


class ReadingForwarder:
    def __init__(self, result: ForwardResult) -> None:
        self.result = result
        self.paths: list[Path] = []
        self.datasets: list[Dataset] = []

    def forward_file(self, path: Path) -> ForwardResult:
        self.paths.append(path)
        self.datasets.append(dcmread(path))
        return self.result


class RecordingMwlClient:
    def __init__(self, payload, *, complete_status=200, complete_error=None):
        self.payload = payload
        self.complete_status = complete_status
        self.complete_error = complete_error
        self.get_calls = 0
        self.complete_calls = 0
        self.complete_payloads = []

    def get_worklist(self):
        self.get_calls += 1
        return type("MwlResponse", (), {"status_code": 200, "payload": self.payload})()

    def complete_worklist(self, payload):
        self.complete_calls += 1
        self.complete_payloads.append(payload)
        if self.complete_error is not None:
            raise self.complete_error
        return type("MwlResponse", (), {"status_code": self.complete_status, "payload": {}})()


class RecordingOrthancClient:
    def __init__(self, study_id="orthanc-study-id") -> None:
        self.study_id = study_id
        self.study_uid_calls: list[str] = []

    def find_study_id_by_uid(self, study_instance_uid):
        self.study_uid_calls.append(study_instance_uid)
        return self.study_id


def test_handle_store_forwarding_disabled_stores_locally_only(tmp_path) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()

    status = handle_store(event, tmp_path)

    assert status == 0x0000
    assert (tmp_path / f"{dataset.SOPInstanceUID}.dcm").exists()


def test_store_dataset_repairs_minimal_file_meta(tmp_path) -> None:
    dataset = _minimal_dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    path = store_dataset(dataset, tmp_path)
    stored = dcmread(path)

    assert stored.file_meta.MediaStorageSOPClassUID == dataset.SOPClassUID
    assert stored.file_meta.MediaStorageSOPInstanceUID == dataset.SOPInstanceUID
    assert stored.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
    assert stored.SOPInstanceUID == dataset.SOPInstanceUID


def test_store_dataset_falls_back_to_original_write_on_strict_failure(
    tmp_path,
    monkeypatch,
) -> None:
    dataset = _minimal_dataset()
    original_save_as = Dataset.save_as
    calls = []

    def fake_save_as(self, filename, write_like_original=True):
        calls.append(write_like_original)
        if write_like_original is False:
            raise ValueError("synthetic strict write failure")
        return original_save_as(self, filename, write_like_original=write_like_original)

    monkeypatch.setattr(Dataset, "save_as", fake_save_as)

    path = store_dataset(dataset, tmp_path)
    stored = dcmread(path, force=True)

    assert calls == [False, True]
    assert stored.SOPInstanceUID == dataset.SOPInstanceUID


def test_handle_store_accepts_minimal_event_file_meta(tmp_path) -> None:
    dataset = _minimal_dataset()
    event_file_meta = FileMetaDataset()
    event_file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": event_file_meta})()

    status = handle_store(event, tmp_path)

    assert status == 0x0000
    assert (tmp_path / f"{dataset.SOPInstanceUID}.dcm").exists()


def test_handle_store_uses_encoded_event_fallback_when_dataset_write_fails(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    buffer = BytesIO()
    dcmwrite(buffer, dataset, write_like_original=False)
    encoded = buffer.getvalue()

    class StoreEvent:
        file_meta = dataset.file_meta

        def __init__(self, stored_dataset):
            self.dataset = stored_dataset

        def encoded_dataset(self, include_meta=True):
            assert include_meta is True
            return encoded

    def fail_store(_dataset, _storage_dir):
        raise ValueError("synthetic strict write failure SHOULD^NOTLOG SECRETID")

    monkeypatch.setattr(dicom_server, "store_dataset", fail_store)

    status = handle_store(StoreEvent(dataset), tmp_path)
    path = tmp_path / f"{dataset.SOPInstanceUID}.dcm"
    stored = dcmread(path, force=True)

    assert status == 0x0000
    assert path.exists()
    assert stored.SOPInstanceUID == dataset.SOPInstanceUID
    assert "ValueError" in caplog.text
    assert "synthetic strict write failure" not in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_does_not_modify_incoming_dataset_tags(tmp_path) -> None:
    dataset = _minimal_dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"
    before = json.loads(dataset.to_json())
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()

    status = handle_store(event, tmp_path)

    assert status == 0x0000
    assert json.loads(dataset.to_json()) == before
    assert dataset.SpecificCharacterSet == "ISO_IR 149"
    assert dataset.PatientName == "SHOULD^NOTLOG"
    assert dataset.PatientID == "SECRETID"


def test_handle_store_writes_charset_inspection_report_and_audit(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    audit_db = tmp_path / "gateway_audit.sqlite3"
    report_path = tmp_path / "dicom_inspection.jsonl"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        audit_db=audit_db,
        inspection_report_path=report_path,
    )

    assert status == 0x0000
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["specific_character_set"] == ["ISO_IR 149"]
    assert payload["needs_charset_review"] is True
    assert payload["text_tag_presence"]["PatientName"] is True
    assert payload["text_tag_presence"]["PatientID"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "SHOULD^NOTLOG" not in serialized
    assert "SECRETID" not in serialized
    assert _dicom_inspection_events(audit_db) == [
        ("dicom_charset_inspected", "ACC-TEST", 1, "review_required"),
    ]
    assert "needs_charset_review=True" in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_charset_fix_disabled_reports_and_forwards_original(
    tmp_path,
) -> None:
    dataset = _minimal_dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = ReadingForwarder(ForwardResult(True, 0x0000))
    audit_db = tmp_path / "gateway_audit.sqlite3"
    fix_report_path = tmp_path / "dicom_charset_fix.jsonl"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        audit_db=audit_db,
        charset_fix_enabled=False,
        charset_fix_mode="iso_ir_149_to_utf8",
        charset_fix_report_path=fix_report_path,
    )

    original_path = tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert forwarder.paths == [original_path]
    assert not (tmp_path / "inbox" / "forwarded").exists()
    report = json.loads(fix_report_path.read_text(encoding="utf-8"))
    assert report["fix_applied"] is False
    assert report["reason"] == "disabled"
    assert _dicom_charset_fix_events(audit_db) == [
        ("dicom_charset_fix_checked", "ACC-TEST", 1, "skipped_disabled"),
    ]


def test_handle_store_default_charset_fix_forwards_normalized_copy(tmp_path) -> None:
    dataset = _minimal_dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"
    dataset.PatientName = "KOREAN^TEST"
    dataset.PatientID = "PID-STABLE"
    dataset.StudyDescription = "KOREAN STUDY"
    step = Dataset()
    step.ScheduledProcedureStepDescription = "KOREAN STEP"
    dataset.ScheduledProcedureStepSequence = Sequence([step])
    dataset.Rows = 1
    dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = b"\x01\x02"
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = ReadingForwarder(ForwardResult(True, 0x0000))
    audit_db = tmp_path / "gateway_audit.sqlite3"
    fix_report_path = tmp_path / "dicom_charset_fix.jsonl"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        audit_db=audit_db,
        charset_fix_report_path=fix_report_path,
    )

    original_path = tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"
    forwarded_path = tmp_path / "inbox" / "forwarded" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert forwarder.paths == [forwarded_path]
    assert original_path.exists()
    assert forwarded_path.exists()
    original = dcmread(original_path)
    forwarded = forwarder.datasets[0]
    assert original.SpecificCharacterSet == "ISO_IR 149"
    assert forwarded.SpecificCharacterSet == "ISO_IR 192"
    assert str(forwarded.SOPInstanceUID) == str(dataset.SOPInstanceUID)
    assert str(forwarded.StudyInstanceUID) == str(dataset.StudyInstanceUID)
    assert str(forwarded.SeriesInstanceUID) == str(dataset.SeriesInstanceUID)
    assert forwarded.AccessionNumber == "ACC-TEST"
    assert forwarded.Modality == "OT"
    assert forwarded.PatientID == "PID-STABLE"
    assert bytes(forwarded.PixelData) == b"\x01\x02"
    report = json.loads(fix_report_path.read_text(encoding="utf-8"))
    assert report["fix_applied"] is True
    assert report["new_specific_character_set"] == ["ISO_IR 192"]
    assert "PatientName" in report["fixed_keywords"]
    assert "PatientID" in report["skipped_keywords"]
    assert "PID-STABLE" not in json.dumps(report, ensure_ascii=False)
    assert _dicom_charset_fix_events(audit_db) == [
        ("dicom_charset_fix_checked", "ACC-TEST", 1, None),
    ]


def test_handle_store_charset_fix_mode_off_forwards_original(tmp_path) -> None:
    dataset = _minimal_dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = ReadingForwarder(ForwardResult(True, 0x0000))
    audit_db = tmp_path / "gateway_audit.sqlite3"
    fix_report_path = tmp_path / "dicom_charset_fix.jsonl"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        audit_db=audit_db,
        charset_fix_enabled=True,
        charset_fix_mode="off",
        charset_fix_report_path=fix_report_path,
    )

    original_path = tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert forwarder.paths == [original_path]
    assert not (tmp_path / "inbox" / "forwarded").exists()
    report = json.loads(fix_report_path.read_text(encoding="utf-8"))
    assert report["fix_applied"] is False
    assert report["reason"] == "mode_off"
    assert _dicom_charset_fix_events(audit_db) == [
        ("dicom_charset_fix_checked", "ACC-TEST", 1, "skipped_mode_off"),
    ]


def test_handle_store_charset_fix_failure_forwards_original_without_failing(
    tmp_path,
    caplog,
    monkeypatch,
) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = ReadingForwarder(ForwardResult(True, 0x0000))
    audit_db = tmp_path / "gateway_audit.sqlite3"
    fix_report_path = tmp_path / "dicom_charset_fix.jsonl"
    init_audit_db(audit_db)

    def fail_fix(*_args, **_kwargs):
        raise RuntimeError("synthetic charset fix failure SHOULD^NOTLOG SECRETID")

    monkeypatch.setattr(dicom_server, "maybe_fix_charset", fail_fix)

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        audit_db=audit_db,
        charset_fix_enabled=True,
        charset_fix_mode="iso_ir_149_to_utf8",
        charset_fix_report_path=fix_report_path,
    )

    original_path = tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert forwarder.paths == [original_path]
    assert _dicom_charset_fix_events(audit_db) == [
        ("dicom_charset_fix_checked", "ACC-TEST", 0, "charset_fix_failed"),
    ]
    report = json.loads(fix_report_path.read_text(encoding="utf-8"))
    assert report["fix_applied"] is False
    assert report["error_code"] == "charset_fix_failed"
    assert "RuntimeError" in caplog.text
    assert "synthetic charset fix failure" not in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_charset_fix_retries_from_stored_file_after_live_failure(
    tmp_path,
    caplog,
    monkeypatch,
) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    if hasattr(dataset, "SpecificCharacterSet"):
        del dataset.SpecificCharacterSet
    dataset.PatientName = "ÀÌÁø¼º"
    dataset.PatientID = "PID-STABLE"
    dataset.StudyDescription = "ÈäºÎ[Á÷Á¢]1¸Å"
    dataset.SeriesDescription = "PA"
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = ReadingForwarder(ForwardResult(True, 0x0000))
    audit_db = tmp_path / "gateway_audit.sqlite3"
    fix_report_path = tmp_path / "dicom_charset_fix.jsonl"
    init_audit_db(audit_db)
    real_fix = dicom_server.maybe_fix_charset
    calls = 0

    def fail_once_then_fix(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("synthetic live dataset failure ÀÌÁø¼º PID-STABLE")
        return real_fix(*args, **kwargs)

    monkeypatch.setattr(dicom_server, "maybe_fix_charset", fail_once_then_fix)

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        audit_db=audit_db,
        charset_fix_report_path=fix_report_path,
    )

    original_path = tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"
    forwarded_path = tmp_path / "inbox" / "forwarded" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert calls == 2
    assert forwarder.paths == [forwarded_path]
    assert original_path.exists()
    assert forwarded_path.exists()
    forwarded = forwarder.datasets[0]
    assert forwarded.SpecificCharacterSet == "ISO_IR 192"
    assert str(forwarded.PatientName) == "이진성"
    assert forwarded.StudyDescription == "흉부[직접]1매"
    assert forwarded.SeriesDescription == "PA"
    assert forwarded.PatientID == "PID-STABLE"
    assert forwarded.AccessionNumber == "ACC-TEST"
    report = json.loads(fix_report_path.read_text(encoding="utf-8"))
    assert report["fix_applied"] is True
    assert report["reason"] == "missing_charset_euc_kr_to_utf8_applied"
    assert report["new_specific_character_set"] == ["ISO_IR 192"]
    assert "PatientName" in report["fixed_keywords"]
    assert _dicom_charset_fix_events(audit_db) == [
        ("dicom_charset_fix_checked", "ACC-TEST", 1, None),
    ]
    assert "ValueError" in caplog.text
    assert "retry from stored file succeeded" in caplog.text
    assert "synthetic live dataset failure" not in caplog.text
    assert "ÀÌÁø¼º" not in caplog.text
    assert "이진성" not in caplog.text
    assert "PID-STABLE" not in caplog.text


def test_handle_store_inspection_failure_does_not_fail_c_store(
    tmp_path,
    caplog,
    monkeypatch,
) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = RecordingForwarder(ForwardResult(True, 0x0000))
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    def fail_inspection(_dataset):
        raise RuntimeError("synthetic inspection failure SHOULD^NOTLOG SECRETID")

    monkeypatch.setattr(dicom_server, "inspect_dataset", fail_inspection)

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        audit_db=audit_db,
        inspection_report_path=tmp_path / "dicom_inspection.jsonl",
    )

    assert status == 0x0000
    assert forwarder.paths == [tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"]
    assert _dicom_inspection_events(audit_db) == [
        ("dicom_charset_inspected", "ACC-TEST", 0, "inspection_failed"),
    ]
    assert "RuntimeError" in caplog.text
    assert "synthetic inspection failure" not in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_queue_disabled_does_not_enqueue(tmp_path) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    queue_db = tmp_path / "gateway_queue.sqlite3"

    status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=queue_db,
        queue_enabled=False,
    )

    assert status == 0x0000
    assert not queue_db.exists()


def test_handle_store_queue_enabled_enqueues_after_local_write(tmp_path) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    queue_db = tmp_path / "gateway_queue.sqlite3"

    status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=queue_db,
        queue_enabled=True,
    )

    stored_path = tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert stored_path.exists()
    assert get_queue_counts(queue_db) == {
        "pending": 1,
        "forwarding": 0,
        "completed": 0,
        "failed": 0,
        "dead_letter": 0,
    }
    with sqlite3.connect(queue_db) as connection:
        row = connection.execute(
            f"""
            SELECT sop_instance_uid, study_instance_uid, accession_number,
                   modality, file_path, status
            FROM {QUEUE_TABLE}
            """
        ).fetchone()
        columns = [
            item[1]
            for item in connection.execute(f"PRAGMA table_info({QUEUE_TABLE})")
        ]
    assert row == (
        str(dataset.SOPInstanceUID),
        str(dataset.StudyInstanceUID),
        "ACC-TEST",
        "OT",
        str(stored_path),
        "pending",
    )
    assert "PatientName" not in columns
    assert "PatientID" not in columns


def test_handle_store_direct_mode_duplicate_sop_keeps_single_queue_row(tmp_path) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    queue_db = tmp_path / "gateway_queue.sqlite3"

    first_status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=queue_db,
        queue_enabled=True,
        forward_mode="direct",
    )
    second_status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=queue_db,
        queue_enabled=True,
        forward_mode="direct",
    )

    assert first_status == 0x0000
    assert second_status == 0x0000
    assert get_queue_counts(queue_db) == {
        "pending": 1,
        "forwarding": 0,
        "completed": 0,
        "failed": 0,
        "dead_letter": 0,
    }


def test_handle_store_queue_mode_enqueues_without_direct_forward_match_or_completion(
    tmp_path,
) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    queue_db = tmp_path / "gateway_queue.sqlite3"
    forwarder = RecordingForwarder(ForwardResult(True, 0x0000))
    mwl_client = RecordingMwlClient(
        {"entries": [{"Active": True, "AccessionNumber": "ACC-TEST"}]}
    )
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        mwl_client=mwl_client,
        audit_db=audit_db,
        queue_db=queue_db,
        queue_enabled=True,
        forward_mode="queue",
    )

    stored_path = tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert stored_path.exists()
    assert forwarder.paths == []
    assert mwl_client.get_calls == 0
    assert mwl_client.complete_calls == 0
    assert get_queue_counts(queue_db) == {
        "pending": 1,
        "forwarding": 0,
        "completed": 0,
        "failed": 0,
        "dead_letter": 0,
    }
    assert _dicom_match_events(audit_db) == []
    assert _dicom_completion_events(audit_db) == []


def test_handle_store_queue_mode_duplicate_sop_does_not_create_duplicate_rows(
    tmp_path,
) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    queue_db = tmp_path / "gateway_queue.sqlite3"

    first_status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=queue_db,
        queue_enabled=True,
        forward_mode="queue",
    )
    second_status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=queue_db,
        queue_enabled=True,
        forward_mode="queue",
    )

    assert first_status == 0x0000
    assert second_status == 0x0000
    assert get_queue_counts(queue_db) == {
        "pending": 1,
        "forwarding": 0,
        "completed": 0,
        "failed": 0,
        "dead_letter": 0,
    }
    with sqlite3.connect(queue_db) as connection:
        rows = connection.execute(
            f"""
            SELECT sop_instance_uid, status
            FROM {QUEUE_TABLE}
            """
        ).fetchall()
    assert rows == [(str(dataset.SOPInstanceUID), "pending")]


def test_handle_store_queue_mode_enqueues_normalized_copy_when_fix_applies(
    tmp_path,
) -> None:
    dataset = _minimal_dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"
    dataset.StudyDescription = "KOREAN STUDY"
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    queue_db = tmp_path / "gateway_queue.sqlite3"

    status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=queue_db,
        queue_enabled=True,
        forward_mode="queue",
        charset_fix_enabled=True,
        charset_fix_mode="iso_ir_149_to_utf8",
        charset_fix_report_path=tmp_path / "dicom_charset_fix.jsonl",
    )

    forwarded_path = tmp_path / "inbox" / "forwarded" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert forwarded_path.exists()
    with sqlite3.connect(queue_db) as connection:
        row = connection.execute(
            f"""
            SELECT file_path, status
            FROM {QUEUE_TABLE}
            """
        ).fetchone()
    assert row == (str(forwarded_path), "pending")
    assert dcmread(forwarded_path).SpecificCharacterSet == "ISO_IR 192"


def test_handle_store_queue_mode_requires_queue_enabled(tmp_path) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()

    status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=tmp_path / "gateway_queue.sqlite3",
        queue_enabled=False,
        forward_mode="queue",
    )

    assert status == WRITE_FAILURE_STATUS


def test_handle_store_queue_mode_enqueue_failure_returns_write_failure(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()

    def fail_enqueue(_queue_db, _dataset, _path):
        raise RuntimeError("synthetic enqueue failure SHOULD^NOTLOG SECRETID")

    monkeypatch.setattr(dicom_server, "enqueue_stored_dataset", fail_enqueue)

    status = handle_store(
        event,
        tmp_path / "inbox",
        queue_db=tmp_path / "gateway_queue.sqlite3",
        queue_enabled=True,
        forward_mode="queue",
    )

    assert status == WRITE_FAILURE_STATUS
    assert (tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm").exists()
    assert "RuntimeError" in caplog.text
    assert "synthetic enqueue failure" not in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_forwarding_enabled_calls_forwarder_after_local_write(tmp_path) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = RecordingForwarder(ForwardResult(True, 0x0000))
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    status = handle_store(event, tmp_path / "inbox", forwarder=forwarder, audit_db=audit_db)

    stored_path = tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm"
    assert status == 0x0000
    assert forwarder.paths == [stored_path]
    assert stored_path.exists()
    assert _dicom_audit_events(audit_db) == [
        ("dicom_store_received", "ACC-TEST", 0, 1, None),
        ("dicom_charset_inspected", "ACC-TEST", None, 1, "review_required"),
        (
            "dicom_charset_fix_checked",
            "ACC-TEST",
            None,
            1,
            "skipped_not_target_charset",
        ),
        ("dicom_forward_success", "ACC-TEST", 0, 1, None),
    ]


def test_handle_store_forwarding_failure_returns_failure_status(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = RecordingForwarder(ForwardResult(False, None, "association_failed"))
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    status = handle_store(event, tmp_path / "inbox", forwarder=forwarder, audit_db=audit_db)

    assert status == WRITE_FAILURE_STATUS
    assert (tmp_path / "inbox" / f"{dataset.SOPInstanceUID}.dcm").exists()
    assert _dicom_audit_events(audit_db) == [
        ("dicom_store_received", "ACC-TEST", 0, 1, None),
        ("dicom_charset_inspected", "ACC-TEST", None, 1, "review_required"),
        (
            "dicom_charset_fix_checked",
            "ACC-TEST",
            None,
            1,
            "skipped_not_target_charset",
        ),
        ("dicom_forward_failed", "ACC-TEST", None, 0, "association_failed"),
    ]
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_success_gets_worklist_and_completes_matched_accession(
    tmp_path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = RecordingForwarder(ForwardResult(True, 0x0000))
    mwl_client = RecordingMwlClient(
        {
            "entries": [
                {
                    "Active": True,
                    "AccessionNumber": "ACC-TEST",
                    "PatientName": "SHOULD^NOTLOG",
                    "PatientID": "SECRETID",
                }
            ]
        }
    )
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        mwl_client=mwl_client,
        audit_db=audit_db,
    )

    assert status == 0x0000
    assert mwl_client.get_calls == 1
    assert mwl_client.complete_calls == 1
    assert mwl_client.complete_payloads == [{"AccessionNumber": "ACC-TEST"}]
    assert _dicom_match_events(audit_db) == [
        ("dicom_match", "ACC-TEST", "AccessionNumber", 1, None),
    ]
    assert _dicom_completion_events(audit_db) == [
        ("dicom_worklist_complete", "ACC-TEST", "AccessionNumber", 1, None),
    ]
    assert "DICOM worklist completion result" in caplog.text
    assert "matched=True" in caplog.text
    assert "AccessionNumber" in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_saves_operational_metadata_after_mwl_match(tmp_path) -> None:
    dataset = _minimal_dataset()
    dataset.Modality = ""
    before = json.loads(dataset.to_json())
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = RecordingForwarder(ForwardResult(True, 0x0000))
    mwl_client = RecordingMwlClient(
        {
            "entries": [
                {
                    "Active": True,
                    "AccessionNumber": "ACC-TEST",
                    "PatientID": "CHART-1",
                    "Modality": "CR",
                    "ScheduledStationAETitle": "INNOVISION",
                    "StudyType": "CR",
                    "PatientName": "SHOULD^NOT_STORE",
                    "PatientBirthDate": "19700101",
                    "PatientSex": "M",
                }
            ]
        }
    )
    orthanc_client = RecordingOrthancClient()
    metadata_db = tmp_path / "gateway_operational_metadata.sqlite3"

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        mwl_client=mwl_client,
        orthanc_client=orthanc_client,
        operational_metadata_db=metadata_db,
    )

    record = get_by_accession_number(metadata_db, "ACC-TEST")
    assert status == 0x0000
    assert record is not None
    assert record.orthanc_study_id == "orthanc-study-id"
    assert record.study_instance_uid == dataset.StudyInstanceUID
    assert record.sop_instance_uid == dataset.SOPInstanceUID
    assert record.patient_id == "CHART-1"
    assert record.dicom_modality_original == ""
    assert record.workflow_modality == "CR"
    assert record.station_aet == "INNOVISION"
    assert record.study_type == "CR"
    assert record.display_modality == "X-ray"
    assert record.aio_domain_candidate == "cxr"
    assert record.matched_by == "AccessionNumber"
    assert record.match_confidence == "exact"
    assert record.source == "gateway_mwl_match"
    assert orthanc_client.study_uid_calls == [str(dataset.StudyInstanceUID)]
    assert json.loads(dataset.to_json()) == before


def test_handle_store_no_match_audits_accession_only_without_demographics(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    mwl_client = RecordingMwlClient(
        {
            "entries": [
                {
                    "Active": True,
                    "AccessionNumber": "OTHER",
                    "PatientName": "SHOULD^NOTLOG",
                    "PatientID": "SECRETID",
                }
            ]
        }
    )
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        mwl_client=mwl_client,
        audit_db=audit_db,
    )

    assert status == 0x0000
    assert mwl_client.get_calls == 1
    assert mwl_client.complete_calls == 0
    assert _dicom_match_events(audit_db) == [
        ("dicom_match", "ACC-TEST", None, 0, "no_active_match"),
    ]
    assert "matched=False" in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_does_not_complete_when_forward_fails(tmp_path) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    forwarder = RecordingForwarder(ForwardResult(False, None, "association_failed"))
    mwl_client = RecordingMwlClient({"entries": [{"Active": True, "AccessionNumber": "ACC-TEST"}]})

    status = handle_store(
        event,
        tmp_path / "inbox",
        forwarder=forwarder,
        mwl_client=mwl_client,
    )

    assert status == WRITE_FAILURE_STATUS
    assert mwl_client.get_calls == 0
    assert mwl_client.complete_calls == 0


def test_handle_store_does_not_complete_when_match_accession_missing(tmp_path) -> None:
    dataset = _minimal_dataset()
    del dataset.AccessionNumber
    dataset.RequestedProcedureID = "RP1"
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    mwl_client = RecordingMwlClient({"entries": [{"Active": True, "RequestedProcedureID": "RP1"}]})
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        mwl_client=mwl_client,
        audit_db=audit_db,
    )

    assert status == 0x0000
    assert mwl_client.get_calls == 1
    assert mwl_client.complete_calls == 0
    assert _dicom_match_events(audit_db) == [
        ("dicom_match", None, "RequestedProcedureID", 1, None),
    ]
    assert _dicom_completion_events(audit_db) == []


def test_handle_store_completion_failure_does_not_fail_c_store(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    mwl_client = RecordingMwlClient(
        {"entries": [{"Active": True, "AccessionNumber": "ACC-TEST"}]},
        complete_status=500,
    )
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        mwl_client=mwl_client,
        audit_db=audit_db,
    )

    assert status == 0x0000
    assert mwl_client.complete_calls == 1
    assert _dicom_completion_events(audit_db) == [
        ("dicom_worklist_complete", "ACC-TEST", "AccessionNumber", 0, "mwl_error"),
    ]
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_handle_store_unexpected_completion_exception_does_not_fail_c_store(
    tmp_path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    mwl_client = RecordingMwlClient(
        {"entries": [{"Active": True, "AccessionNumber": "ACC-TEST"}]},
        complete_error=RuntimeError("unexpected completion failure SHOULD^NOTLOG SECRETID"),
    )
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)

    status = handle_store(
        event,
        tmp_path / "inbox",
        mwl_client=mwl_client,
        audit_db=audit_db,
    )

    assert status == 0x0000
    assert mwl_client.complete_calls == 1
    assert _dicom_completion_events(audit_db) == [
        (
            "dicom_worklist_complete",
            "ACC-TEST",
            "AccessionNumber",
            0,
            "completion_failed",
        ),
    ]
    assert "RuntimeError" in caplog.text
    assert "unexpected completion failure" not in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def test_enabled_loopback_c_store_writes_file_without_phi_logs(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    port = _free_loopback_port()
    server = GatewayDicomServer(
        bind="127.0.0.1",
        port=port,
        aet="KAOSPACS_GW_TEST",
        storage_dir=tmp_path,
    ).start()
    dataset = _minimal_dataset()
    ae = AE(ae_title="KAOSPACS_TEST")
    ae.add_requested_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)

    try:
        association = ae.associate("127.0.0.1", port, ae_title="KAOSPACS_GW_TEST")
        assert association.is_established
        status = association.send_c_store(dataset)
        association.release()

        assert status.Status == 0x0000
        stored_path = Path(tmp_path) / f"{dataset.SOPInstanceUID}.dcm"
        assert stored_path.exists()
        assert str(dataset.SOPInstanceUID) in caplog.text
        assert "ACC-TEST" in caplog.text
        assert "OT" in caplog.text
        assert "SHOULD^NOTLOG" not in caplog.text
        assert "SECRETID" not in caplog.text
    finally:
        server.stop()


def test_called_ae_must_match_gateway_aet(tmp_path) -> None:
    port = _free_loopback_port()
    server = GatewayDicomServer(
        bind="127.0.0.1",
        port=port,
        aet="VIEWREX",
        storage_dir=tmp_path,
    ).start()
    ae = AE(ae_title="KAOSPACS_TEST")
    ae.add_requested_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)

    try:
        association = ae.associate("127.0.0.1", port, ae_title="WRONG_AET")

        assert not association.is_established
        assert list(tmp_path.iterdir()) == []
    finally:
        server.stop()


def test_start_dicom_listener_uses_production_gateway_identity(tmp_path) -> None:
    port = _free_loopback_port()
    config = GatewayConfig(
        gateway_dicom_enabled=True,
        gateway_dicom_bind="127.0.0.1",
        gateway_dicom_port=port,
        gateway_dicom_storage_dir=tmp_path,
    )

    server = start_dicom_listener(config)

    try:
        assert server is not None
        assert server.aet == "VIEWREX"
        assert server.port == port
        assert server.bind == "127.0.0.1"
    finally:
        if server is not None:
            server.stop()


def test_forwarder_success_with_local_test_scp(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    target_port = _free_loopback_port()
    target_storage = tmp_path / "target"
    target_server = GatewayDicomServer(
        bind="127.0.0.1",
        port=target_port,
        aet="ORTHANC_TEST",
        storage_dir=target_storage,
    ).start()
    source_storage = tmp_path / "source"
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()
    status = handle_store(event, source_storage)
    assert status == 0x0000
    forwarder = DicomForwarder(
        host="127.0.0.1",
        port=target_port,
        target_aet="ORTHANC_TEST",
        calling_aet="KAOSPACS_GW",
        timeout_seconds=2,
    )

    try:
        result = forwarder.forward_file(source_storage / f"{dataset.SOPInstanceUID}.dcm")

        assert result.success is True
        assert result.status_code == 0x0000
        assert (target_storage / f"{dataset.SOPInstanceUID}.dcm").exists()
        assert "ORTHANC_TEST" in caplog.text
        assert "SHOULD^NOTLOG" not in caplog.text
        assert "SECRETID" not in caplog.text
    finally:
        target_server.stop()


def test_forwarder_unavailable_returns_failure_without_raising(caplog) -> None:
    caplog.set_level(logging.INFO)
    forwarder = DicomForwarder(
        host="127.0.0.1",
        port=1,
        target_aet="ORTHANC_TEST",
        calling_aet="KAOSPACS_GW",
        timeout_seconds=0.2,
    )

    result = forwarder.forward_dataset(_minimal_dataset())

    assert result.success is False
    assert result.error in {"association_failed", "forward_unavailable"}
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text


def _dicom_audit_events(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT event_type, accession_number, status_code, success, error_code
            FROM gateway_events
            ORDER BY id
            """
        ).fetchall()


def _dicom_match_events(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT event_type, accession_number, matched_by, success, error_code
            FROM gateway_events
            WHERE event_type = 'dicom_match'
            ORDER BY id
            """
        ).fetchall()


def _dicom_completion_events(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT event_type, accession_number, matched_by, success, error_code
            FROM gateway_events
            WHERE event_type = 'dicom_worklist_complete'
            ORDER BY id
            """
        ).fetchall()


def _dicom_inspection_events(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT event_type, accession_number, success, error_code
            FROM gateway_events
            WHERE event_type = 'dicom_charset_inspected'
            ORDER BY id
            """
        ).fetchall()


def _dicom_charset_fix_events(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT event_type, accession_number, success, error_code
            FROM gateway_events
            WHERE event_type = 'dicom_charset_fix_checked'
            ORDER BY id
            """
        ).fetchall()
