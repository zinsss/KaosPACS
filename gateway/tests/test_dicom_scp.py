import logging
import socket
import sqlite3
from pathlib import Path

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from pynetdicom import AE

from app.config import GatewayConfig
from app.dicom import server as dicom_server
from app.dicom.forwarder import DicomForwarder, ForwardResult
from app.dicom.server import (
    WRITE_FAILURE_STATUS,
    GatewayDicomServer,
    handle_store,
    start_dicom_listener,
)
from app.dicom.storage import safe_dicom_filename
from app.services.audit import init_audit_db


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
    assert "PatientName" not in caplog.text
    assert "PatientID" not in caplog.text


class RecordingForwarder:
    def __init__(self, result: ForwardResult) -> None:
        self.result = result
        self.paths: list[Path] = []

    def forward_file(self, path: Path) -> ForwardResult:
        self.paths.append(path)
        return self.result


def test_handle_store_forwarding_disabled_stores_locally_only(tmp_path) -> None:
    dataset = _minimal_dataset()
    event = type("StoreEvent", (), {"dataset": dataset, "file_meta": dataset.file_meta})()

    status = handle_store(event, tmp_path)

    assert status == 0x0000
    assert (tmp_path / f"{dataset.SOPInstanceUID}.dcm").exists()


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
        ("dicom_forward_failed", "ACC-TEST", None, 0, "association_failed"),
    ]
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text
    assert "PatientName" not in caplog.text
    assert "PatientID" not in caplog.text


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
        assert "PatientName" not in caplog.text
        assert "PatientID" not in caplog.text
    finally:
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
        assert "PatientName" not in caplog.text
        assert "PatientID" not in caplog.text
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
    assert "PatientName" not in caplog.text
    assert "PatientID" not in caplog.text


def _dicom_audit_events(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT event_type, accession_number, status_code, success, error_code
            FROM gateway_events
            ORDER BY id
            """
        ).fetchall()
