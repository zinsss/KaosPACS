import sqlite3
from pathlib import Path

from pydicom.dataset import Dataset

from app.dicom.queue import (
    QUEUE_TABLE,
    enqueue_stored_dataset,
    get_pending_rows,
    get_queue_counts,
    init_queue_db,
    mark_completed,
    mark_dead_letter,
    mark_failed,
    mark_forwarding,
    requeue_due_failed_rows,
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

    result = enqueue_stored_dataset(db_path, _dataset(), file_path)

    assert result.queue_id == 1
    assert result.inserted is True
    assert result.status == "pending"
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
    first_id = enqueue_stored_dataset(db_path, _dataset_with_sop("1.2.3.1"), tmp_path / "one.dcm").queue_id
    second_id = enqueue_stored_dataset(db_path, _dataset_with_sop("1.2.3.2"), tmp_path / "two.dcm").queue_id
    third_id = enqueue_stored_dataset(db_path, _dataset_with_sop("1.2.3.3"), tmp_path / "three.dcm").queue_id
    fourth_id = enqueue_stored_dataset(db_path, _dataset_with_sop("1.2.3.4"), tmp_path / "four.dcm").queue_id

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


def test_due_failed_rows_requeue_to_pending(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    queue_id = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "one.dcm").queue_id
    mark_forwarding(db_path, queue_id)
    mark_failed(
        db_path,
        queue_id,
        last_error="association_failed",
        next_attempt_at="2000-01-01T00:00:00+00:00",
    )

    requeue_due_failed_rows(db_path, max_attempts=10)

    rows = get_pending_rows(db_path)
    assert len(rows) == 1
    assert rows[0].id == queue_id
    assert rows[0].attempts == 1


def test_enqueue_same_sop_returns_existing_row_without_duplicate(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    first = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "one.dcm")
    second = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "two.dcm")

    assert first.queue_id == second.queue_id
    assert first.inserted is True
    assert second.inserted is False
    assert second.status == "pending"
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, sop_instance_uid, file_path, status
            FROM {QUEUE_TABLE}
            """
        ).fetchall()
    assert rows == [(1, "1.2.3.4", str(tmp_path / "two.dcm"), "pending")]


def test_duplicate_enqueue_does_not_store_demographic_columns(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    enqueue_stored_dataset(db_path, _dataset(), tmp_path / "one.dcm")
    enqueue_stored_dataset(db_path, _dataset(), tmp_path / "two.dcm")

    columns = _columns(db_path)
    assert "PatientName" not in columns
    assert "PatientID" not in columns
    assert "PatientBirthDate" not in columns
    assert "PatientSex" not in columns


def test_completed_duplicate_enqueue_does_not_reset_to_pending(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    first = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "one.dcm")
    mark_completed(db_path, first.queue_id)

    duplicate = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "two.dcm")

    assert duplicate.queue_id == first.queue_id
    assert duplicate.inserted is False
    assert duplicate.status == "completed"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            f"""
            SELECT status, file_path
            FROM {QUEUE_TABLE}
            WHERE id = ?
            """,
            (first.queue_id,),
        ).fetchone()
    assert row == ("completed", str(tmp_path / "one.dcm"))


def test_existing_duplicate_sops_are_deduplicated_before_unique_index(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"""
            CREATE TABLE {QUEUE_TABLE} (
                id INTEGER PRIMARY KEY,
                sop_instance_uid TEXT,
                study_instance_uid TEXT,
                accession_number TEXT,
                modality TEXT,
                file_path TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO {QUEUE_TABLE} (
                sop_instance_uid, study_instance_uid, accession_number, modality,
                file_path, status, attempts, created_at, updated_at
            )
            VALUES
                ('1.2.3.4', '1.2.3', 'ACC-QUEUE', 'OT', '/tmp/one.dcm', 'pending', 0, '2026-01-01', '2026-01-01'),
                ('1.2.3.4', '1.2.3', 'ACC-QUEUE', 'OT', '/tmp/two.dcm', 'pending', 0, '2026-01-01', '2026-01-01')
            """
        )

    init_queue_db(db_path)

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, sop_instance_uid, status, last_error
            FROM {QUEUE_TABLE}
            ORDER BY id
            """
        ).fetchall()
        indexes = connection.execute(
            f"PRAGMA index_list({QUEUE_TABLE})"
        ).fetchall()
    assert rows == [
        (1, "1.2.3.4", "pending", None),
        (2, None, "dead_letter", "duplicate_sop_instance_uid"),
    ]
    assert any(row[1] == f"idx_{QUEUE_TABLE}_sop_instance_uid_unique" for row in indexes)


def _dataset_with_sop(sop_instance_uid: str) -> Dataset:
    dataset = _dataset()
    dataset.SOPInstanceUID = sop_instance_uid
    return dataset
