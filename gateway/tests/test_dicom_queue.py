import sqlite3
from pathlib import Path

from pydicom.dataset import Dataset

from app.dicom.queue import (
    QUEUE_TABLE,
    enqueue_stored_dataset,
    get_queue_counts,
    init_queue_db,
    mark_completed,
    mark_dead_letter,
    mark_failed,
    mark_forwarding,
)


def _dataset() -> Dataset:
    dataset = Dataset()
    dataset.SOPInstanceUID = "1.2.3.4"
    dataset.StudyInstanceUID = "1.2.3"
    dataset.AccessionNumber = "ACC-QUEUE"
    dataset.Modality = "OT"
    dataset.PatientName = "SHOULD^NOTSTORE"
    dataset.PatientID = "SECRETID"
    dataset.PatientBirthDate = "19700101"
    dataset.PatientSex = "O"
    return dataset


def _columns(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        return [
            row[1]
            for row in connection.execute(f"PRAGMA table_info({QUEUE_TABLE})")
        ]


def test_queue_db_initializes_schema(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"

    init_queue_db(db_path)

    assert _columns(db_path) == [
        "id",
        "sop_instance_uid",
        "study_instance_uid",
        "accession_number",
        "modality",
        "file_path",
        "status",
        "attempts",
        "last_error",
        "next_attempt_at",
        "created_at",
        "updated_at",
    ]


def test_enqueue_stores_only_allowed_fields(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    file_path = tmp_path / "dicom-inbox" / "1.2.3.4.dcm"

    queue_id = enqueue_stored_dataset(db_path, _dataset(), file_path)

    assert queue_id == 1
    assert "PatientName" not in _columns(db_path)
    assert "PatientID" not in _columns(db_path)
    assert "PatientBirthDate" not in _columns(db_path)
    assert "PatientSex" not in _columns(db_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            f"""
            SELECT sop_instance_uid, study_instance_uid, accession_number,
                   modality, file_path, status, attempts, last_error
            FROM {QUEUE_TABLE}
            """
        ).fetchone()

    assert row == (
        "1.2.3.4",
        "1.2.3",
        "ACC-QUEUE",
        "OT",
        str(file_path),
        "pending",
        0,
        None,
    )


def test_queue_counts_include_all_statuses(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    first_id = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "one.dcm")
    second_id = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "two.dcm")
    third_id = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "three.dcm")
    fourth_id = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "four.dcm")

    mark_forwarding(db_path, first_id)
    mark_completed(db_path, second_id)
    mark_failed(db_path, third_id, last_error="association_failed")
    mark_dead_letter(db_path, fourth_id, last_error="max_attempts")

    assert get_queue_counts(db_path) == {
        "pending": 0,
        "forwarding": 1,
        "completed": 1,
        "failed": 1,
        "dead_letter": 1,
    }
