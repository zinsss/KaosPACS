import sqlite3
from pathlib import Path

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from app.services.operational_metadata import (
    TABLE,
    build_operational_metadata_record,
    derive_display_modality,
    get_by_accession_number,
    get_by_orthanc_study_id,
    init_operational_metadata_db,
    save_operational_metadata,
)


def _dataset(*, modality: str = "") -> Dataset:
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    dataset.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = dataset.file_meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.AccessionNumber = "ACC-XRAY"
    dataset.Modality = modality
    dataset.PatientName = "SHOULD^NOT_STORE"
    dataset.PatientID = "DICOM-PATIENT-ID"
    return dataset


def _worklist_entry(**overrides):
    entry = {
        "AccessionNumber": "ACC-XRAY",
        "PatientID": "CHART-1",
        "Modality": "CR",
        "ScheduledStationAETitle": "INNOVISION",
        "StudyType": "CR",
    }
    entry.update(overrides)
    return entry


def test_derive_display_modality_mappings() -> None:
    assert derive_display_modality(station_aet="INNOVISION", workflow_modality="", study_type="") == (
        "X-ray",
        "cxr",
    )
    assert derive_display_modality(station_aet="", workflow_modality="CR", study_type="") == (
        "X-ray",
        "cxr",
    )
    assert derive_display_modality(station_aet="BMD", workflow_modality="", study_type="") == (
        "BMD",
        "bmd",
    )
    assert derive_display_modality(station_aet="", workflow_modality="ECG", study_type="") == (
        "ECG",
        "ecg",
    )
    assert derive_display_modality(station_aet="", workflow_modality="", study_type="") == (
        "Unknown",
        "unsupported",
    )


def test_build_record_preserves_original_dicom_modality_without_modifying_dataset() -> None:
    dataset = _dataset(modality="DX")
    before = dataset.to_json()

    record = build_operational_metadata_record(
        dataset=dataset,
        worklist_entry=_worklist_entry(Modality="CR", ScheduledStationAETitle="INNOVISION"),
        matched_by="AccessionNumber",
        orthanc_study_id="orthanc-study-id",
    )

    assert record.dicom_modality_original == "DX"
    assert record.workflow_modality == "CR"
    assert record.station_aet == "INNOVISION"
    assert record.display_modality == "X-ray"
    assert record.aio_domain_candidate == "cxr"
    assert record.patient_id == "CHART-1"
    assert record.matched_by == "AccessionNumber"
    assert record.match_confidence == "exact"
    assert record.source == "gateway_mwl_match"
    assert dataset.to_json() == before


def test_save_and_lookup_operational_metadata_without_demographic_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway_operational_metadata.sqlite3"
    dataset = _dataset(modality="")
    record = save_operational_metadata(
        db_path,
        dataset=dataset,
        worklist_entry=_worklist_entry(),
        matched_by="AccessionNumber",
        orthanc_study_id="orthanc-study-id",
    )

    assert record is not None
    by_study = get_by_orthanc_study_id(db_path, "orthanc-study-id")
    by_accession = get_by_accession_number(db_path, "ACC-XRAY")
    assert by_study == by_accession
    assert by_accession is not None
    assert by_accession.display_modality == "X-ray"
    assert by_accession.aio_domain_candidate == "cxr"

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({TABLE})")
        }
    assert "patient_name" not in columns
    assert "patient_birth_date" not in columns
    assert "patient_sex" not in columns
    assert "diagnosis" not in columns
    assert "payload" not in columns


def test_bmd_and_ecg_records_map_to_expected_domains(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway_operational_metadata.sqlite3"
    bmd_dataset = _dataset(modality="")
    bmd_dataset.AccessionNumber = "ACC-BMD"
    save_operational_metadata(
        db_path,
        dataset=bmd_dataset,
        worklist_entry=_worklist_entry(
            AccessionNumber="ACC-BMD",
            Modality="BMD",
            ScheduledStationAETitle="BMD",
            StudyType="BMD",
        ),
        matched_by="AccessionNumber",
    )
    ecg_dataset = _dataset(modality="")
    ecg_dataset.AccessionNumber = "ACC-ECG"
    save_operational_metadata(
        db_path,
        dataset=ecg_dataset,
        worklist_entry=_worklist_entry(
            AccessionNumber="ACC-ECG",
            Modality="ECG",
            ScheduledStationAETitle="ECG",
            StudyType="ECG",
        ),
        matched_by="AccessionNumber",
    )

    bmd = get_by_accession_number(db_path, "ACC-BMD")
    ecg = get_by_accession_number(db_path, "ACC-ECG")
    assert bmd is not None
    assert bmd.display_modality == "BMD"
    assert bmd.aio_domain_candidate == "bmd"
    assert ecg is not None
    assert ecg.display_modality == "ECG"
    assert ecg.aio_domain_candidate == "ecg"


def test_init_operational_metadata_db_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway_operational_metadata.sqlite3"

    init_operational_metadata_db(db_path)

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone()
    assert row == (TABLE,)
