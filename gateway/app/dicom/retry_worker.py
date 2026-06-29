from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.config import GatewayConfig
from app.dicom.forwarder import DicomForwarder, ForwardResult
from app.dicom.queue import (
    DicomQueueRow,
    get_pending_rows,
    mark_completed,
    mark_dead_letter,
    mark_failed,
    mark_forwarding,
    requeue_due_failed_rows,
)


LOGGER = logging.getLogger("kaospacs.gateway.dicom.retry_worker")

_ACTIVE_WORKER: DicomQueueRetryWorker | None = None


def retry_delay_seconds(next_attempt_number: int) -> int:
    if next_attempt_number <= 1:
        return 0
    if next_attempt_number == 2:
        return 30
    if next_attempt_number == 3:
        return 60
    return 300


class DicomQueueRetryWorker:
    def __init__(
        self,
        *,
        queue_db: Path,
        forwarder: DicomForwarder,
        poll_interval_seconds: float,
        max_attempts: int,
        batch_size: int = 10,
    ) -> None:
        self.queue_db = queue_db
        self.forwarder = forwarder
        self.poll_interval_seconds = poll_interval_seconds
        self.max_attempts = max_attempts
        self.batch_size = batch_size
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "DicomQueueRetryWorker":
        if self.running:
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="gateway-dicom-queue-retry-worker",
            daemon=False,
        )
        self._thread.start()
        LOGGER.info(
            "DICOM queue retry worker started poll_interval_seconds=%s max_attempts=%s",
            self.poll_interval_seconds,
            self.max_attempts,
        )
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        LOGGER.info("DICOM queue retry worker stopped")

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_interval_seconds):
            try:
                self.process_once()
            except Exception as error:
                LOGGER.warning(
                    "DICOM queue retry worker iteration failed exception=%s",
                    error.__class__.__name__,
                )

    def process_once(self, now: datetime | None = None) -> int:
        current_time = now or datetime.now().astimezone()
        requeue_due_failed_rows(
            self.queue_db,
            now=current_time,
            max_attempts=self.max_attempts,
        )
        rows = get_pending_rows(
            self.queue_db,
            now=current_time,
            limit=self.batch_size,
        )
        for row in rows:
            self._process_row(row, current_time)
        return len(rows)

    def _process_row(self, row: DicomQueueRow, now: datetime) -> None:
        if row.attempts >= self.max_attempts:
            mark_dead_letter(
                self.queue_db,
                row.id,
                last_error=row.last_error or "max_attempts",
            )
            LOGGER.warning(
                "DICOM queue row dead-lettered queue_id=%s sop_instance_uid=%s "
                "study_instance_uid=%s accession_number=%s modality=%s attempts=%s",
                row.id,
                row.sop_instance_uid or "",
                row.study_instance_uid or "",
                row.accession_number or "",
                row.modality or "",
                row.attempts,
            )
            return

        attempt_number = row.attempts + 1
        mark_forwarding(self.queue_db, row.id)
        LOGGER.info(
            "DICOM queue forwarding started queue_id=%s sop_instance_uid=%s "
            "study_instance_uid=%s accession_number=%s modality=%s attempt=%s",
            row.id,
            row.sop_instance_uid or "",
            row.study_instance_uid or "",
            row.accession_number or "",
            row.modality or "",
            attempt_number,
        )

        result = self._forward(row)
        if result.success:
            mark_completed(self.queue_db, row.id)
            LOGGER.info(
                "DICOM queue forwarding completed queue_id=%s sop_instance_uid=%s "
                "study_instance_uid=%s accession_number=%s modality=%s attempt=%s status=%s",
                row.id,
                row.sop_instance_uid or "",
                row.study_instance_uid or "",
                row.accession_number or "",
                row.modality or "",
                attempt_number,
                result.status_code,
            )
            return

        error_code = result.error or "forward_failed"
        if attempt_number >= self.max_attempts:
            mark_dead_letter(self.queue_db, row.id, last_error=error_code)
            LOGGER.warning(
                "DICOM queue forwarding dead-letter queue_id=%s sop_instance_uid=%s "
                "study_instance_uid=%s accession_number=%s modality=%s attempt=%s error=%s",
                row.id,
                row.sop_instance_uid or "",
                row.study_instance_uid or "",
                row.accession_number or "",
                row.modality or "",
                attempt_number,
                error_code,
            )
            return

        next_attempt_number = attempt_number + 1
        next_attempt_at = (
            now + timedelta(seconds=retry_delay_seconds(next_attempt_number))
        ).isoformat(timespec="seconds")
        mark_failed(
            self.queue_db,
            row.id,
            last_error=error_code,
            next_attempt_at=next_attempt_at,
        )
        LOGGER.warning(
            "DICOM queue forwarding failed queue_id=%s sop_instance_uid=%s "
            "study_instance_uid=%s accession_number=%s modality=%s attempt=%s "
            "status=failed next_attempt_at=%s error=%s",
            row.id,
            row.sop_instance_uid or "",
            row.study_instance_uid or "",
            row.accession_number or "",
            row.modality or "",
            attempt_number,
            next_attempt_at,
            error_code,
        )

    def _forward(self, row: DicomQueueRow) -> ForwardResult:
        try:
            return self.forwarder.forward_file(row.file_path)
        except Exception as error:
            LOGGER.warning(
                "DICOM queue forward call failed queue_id=%s sop_instance_uid=%s "
                "study_instance_uid=%s accession_number=%s modality=%s exception=%s",
                row.id,
                row.sop_instance_uid or "",
                row.study_instance_uid or "",
                row.accession_number or "",
                row.modality or "",
                error.__class__.__name__,
            )
            return ForwardResult(False, None, "forward_exception")


def start_queue_retry_worker(config: GatewayConfig) -> DicomQueueRetryWorker | None:
    global _ACTIVE_WORKER
    if not config.gateway_queue_worker_enabled:
        _ACTIVE_WORKER = None
        LOGGER.info("DICOM queue retry worker disabled")
        return None

    worker = DicomQueueRetryWorker(
        queue_db=config.gateway_queue_db,
        forwarder=DicomForwarder(
            host=config.orthanc_dicom_host,
            port=config.orthanc_dicom_port,
            target_aet=config.orthanc_dicom_aet,
            calling_aet=config.gateway_forwarding_aet,
            timeout_seconds=config.gateway_dicom_forward_timeout_seconds,
        ),
        poll_interval_seconds=config.gateway_queue_poll_interval_seconds,
        max_attempts=config.gateway_queue_max_attempts,
    ).start()
    _ACTIVE_WORKER = worker
    return worker


def is_queue_retry_worker_running() -> bool:
    return _ACTIVE_WORKER is not None and _ACTIVE_WORKER.running
