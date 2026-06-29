import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from pydicom.dataset import Dataset

from app.config import GatewayConfig
from app.dicom.forwarder import ForwardResult
from app.dicom.queue import QUEUE_TABLE, enqueue_stored_dataset, get_queue_counts
from app.dicom.retry_worker import (
    DicomQueueRetryWorker,
    retry_delay_seconds,
    start_queue_retry_worker,
)


class RecordingForwarder:
    def __init__(self, *results: ForwardResult) -> None:
        self.results = list(results)
        self.paths: list[Path] = []

    def forward_file(self, path: Path) -> ForwardResult:
        self.paths.append(path)
        if self.results:
            return self.results.pop(0)
        return ForwardResult(True, 0x0000)


def _dataset() -> Dataset:
    dataset = Dataset()
    dataset.SOPInstanceUID = "1.2.3.4"
    dataset.StudyInstanceUID = "1.2.3"
    dataset.AccessionNumber = "ACC-QUEUE"
    dataset.Modality = "OT"
    dataset.PatientName = "SHOULD^NOTLOG"
    dataset.PatientID = "SECRETID"
    dataset.PatientBirthDate = "19700101"
    dataset.PatientSex = "O"
    return dataset


def _enqueue(db_path: Path, file_path: Path | None = None) -> int:
    return enqueue_stored_dataset(
        db_path,
        _dataset(),
        file_path or Path("/tmp/test.dcm"),
    ).queue_id


def _row(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            f"""
            SELECT status, attempts, last_error, next_attempt_at
            FROM {QUEUE_TABLE}
            ORDER BY id
            """
        ).fetchone()


def test_retry_delay_schedule() -> None:
    assert retry_delay_seconds(1) == 0
    assert retry_delay_seconds(2) == 30
    assert retry_delay_seconds(3) == 60
    assert retry_delay_seconds(4) == 300
    assert retry_delay_seconds(10) == 300


def test_worker_disabled_default_does_not_start() -> None:
    worker = start_queue_retry_worker(GatewayConfig())

    assert worker is None


def test_worker_enabled_starts_thread(tmp_path) -> None:
    worker = DicomQueueRetryWorker(
        queue_db=tmp_path / "gateway_queue.sqlite3",
        forwarder=RecordingForwarder(),
        poll_interval_seconds=60,
        max_attempts=10,
    )

    try:
        worker.start()
        assert worker.running is True
    finally:
        worker.stop()

    assert worker.running is False


def test_pending_row_successful_forward_marks_completed(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    file_path = tmp_path / "one.dcm"
    _enqueue(db_path, file_path)
    forwarder = RecordingForwarder(ForwardResult(True, 0x0000))
    worker = DicomQueueRetryWorker(
        queue_db=db_path,
        forwarder=forwarder,
        poll_interval_seconds=5,
        max_attempts=10,
    )

    processed = worker.process_once()

    assert processed == 1
    assert forwarder.paths == [file_path]
    assert _row(db_path) == ("completed", 1, None, None)
    assert get_queue_counts(db_path)["completed"] == 1


def test_worker_does_not_reforward_completed_duplicate_row(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    file_path = tmp_path / "one.dcm"
    first = enqueue_stored_dataset(db_path, _dataset(), file_path)
    worker = DicomQueueRetryWorker(
        queue_db=db_path,
        forwarder=RecordingForwarder(ForwardResult(True, 0x0000)),
        poll_interval_seconds=5,
        max_attempts=10,
    )
    worker.process_once()
    duplicate = enqueue_stored_dataset(db_path, _dataset(), tmp_path / "two.dcm")
    forwarder = RecordingForwarder(ForwardResult(True, 0x0000))
    worker = DicomQueueRetryWorker(
        queue_db=db_path,
        forwarder=forwarder,
        poll_interval_seconds=5,
        max_attempts=10,
    )

    processed = worker.process_once()

    assert duplicate.queue_id == first.queue_id
    assert duplicate.inserted is False
    assert duplicate.status == "completed"
    assert processed == 0
    assert forwarder.paths == []
    assert get_queue_counts(db_path)["completed"] == 1


def test_failed_forward_marks_failed_and_increments_attempts(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    _enqueue(db_path)
    forwarder = RecordingForwarder(ForwardResult(False, None, "association_failed"))
    worker = DicomQueueRetryWorker(
        queue_db=db_path,
        forwarder=forwarder,
        poll_interval_seconds=5,
        max_attempts=10,
    )

    processed = worker.process_once(datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert processed == 1
    status, attempts, last_error, next_attempt_at = _row(db_path)
    assert status == "failed"
    assert attempts == 1
    assert last_error == "association_failed"
    assert next_attempt_at == "2026-01-01T00:00:30+00:00"
    assert get_queue_counts(db_path)["failed"] == 1


def test_due_failed_row_is_retried_and_completed(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    _enqueue(db_path)
    forwarder = RecordingForwarder(
        ForwardResult(False, None, "association_failed"),
        ForwardResult(True, 0x0000),
    )
    worker = DicomQueueRetryWorker(
        queue_db=db_path,
        forwarder=forwarder,
        poll_interval_seconds=5,
        max_attempts=10,
    )
    worker.process_once(datetime(2026, 1, 1, tzinfo=timezone.utc))

    processed = worker.process_once(datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc))

    assert processed == 1
    assert _row(db_path) == ("completed", 2, None, None)


def test_dead_letter_after_max_attempts(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    _enqueue(db_path)
    forwarder = RecordingForwarder(ForwardResult(False, None, "association_failed"))
    worker = DicomQueueRetryWorker(
        queue_db=db_path,
        forwarder=forwarder,
        poll_interval_seconds=5,
        max_attempts=1,
    )

    processed = worker.process_once(datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert processed == 1
    assert _row(db_path) == ("dead_letter", 1, "association_failed", None)
    assert get_queue_counts(db_path)["dead_letter"] == 1


def test_worker_logs_do_not_include_demographics(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    db_path = tmp_path / "gateway_queue.sqlite3"
    _enqueue(db_path)
    worker = DicomQueueRetryWorker(
        queue_db=db_path,
        forwarder=RecordingForwarder(ForwardResult(False, None, "association_failed")),
        poll_interval_seconds=5,
        max_attempts=10,
    )

    worker.process_once(datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert "ACC-QUEUE" in caplog.text
    assert "1.2.3.4" in caplog.text
    assert "SHOULD^NOTLOG" not in caplog.text
    assert "SECRETID" not in caplog.text
    assert "PatientName" not in caplog.text
    assert "PatientID" not in caplog.text


def test_worker_thread_can_process_pending_row(tmp_path) -> None:
    db_path = tmp_path / "gateway_queue.sqlite3"
    _enqueue(db_path)
    worker = DicomQueueRetryWorker(
        queue_db=db_path,
        forwarder=RecordingForwarder(ForwardResult(True, 0x0000)),
        poll_interval_seconds=0.01,
        max_attempts=10,
    )

    try:
        worker.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if get_queue_counts(db_path)["completed"] == 1:
                break
            time.sleep(0.02)
        assert get_queue_counts(db_path)["completed"] == 1
    finally:
        worker.stop()
