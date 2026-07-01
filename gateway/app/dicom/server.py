from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydicom.uid import ExplicitVRBigEndian, ExplicitVRLittleEndian, ImplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.presentation import AllStoragePresentationContexts

from app.clients.mwl import MwlApiClient, MwlHttpError, MwlUnavailableError
from app.config import GatewayConfig
from app.dicom.charset_fix import (
    CharsetFixResult,
    append_charset_fix_report,
    failure_result,
    log_charset_fix_result,
    maybe_fix_charset,
)
from app.dicom.completion import CompletionResult, complete_matched_worklist
from app.dicom.forwarder import DicomForwarder
from app.dicom.inspection import (
    append_inspection_report,
    inspect_dataset,
    log_inspection_report,
)
from app.dicom.matcher import MatchResult, match_dataset_to_worklist
from app.dicom.queue import enqueue_stored_dataset
from app.dicom.storage import store_dataset
from app.services.audit import record_gateway_event


LOGGER = logging.getLogger("kaospacs.gateway.dicom")
SUCCESS_STATUS = 0x0000
WRITE_FAILURE_STATUS = 0xA700
TRANSFER_SYNTAXES = [
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    ExplicitVRBigEndian,
]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _ae_title(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="ignore").strip()
    return _text(value)


def _remote_ip(event: evt.Event) -> str:
    try:
        return _text(event.assoc.requestor.address)
    except AttributeError:
        return ""


def _calling_ae(event: evt.Event) -> str:
    try:
        return _ae_title(event.assoc.requestor.ae_title)
    except AttributeError:
        return ""


def _called_ae(event: evt.Event) -> str:
    try:
        return _ae_title(event.assoc.acceptor.ae_title)
    except AttributeError:
        return ""


def handle_store(
    event: evt.Event,
    storage_dir: Path,
    *,
    forwarder: DicomForwarder | None = None,
    mwl_client: MwlApiClient | None = None,
    audit_db: Path | None = None,
    queue_db: Path | None = None,
    queue_enabled: bool = False,
    forward_mode: str = "direct",
    inspection_enabled: bool = True,
    inspection_report_path: Path | None = None,
    charset_fix_enabled: bool = True,
    charset_fix_mode: str = "iso_ir_149_to_utf8",
    charset_fix_report_path: Path | None = None,
) -> int:
    dataset = event.dataset.copy()
    dataset.file_meta = event.file_meta

    try:
        path = store_dataset(dataset, storage_dir)
    except Exception as error:
        LOGGER.error(
            "C-STORE write failed sop_instance_uid=%s exception=%s",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            error.__class__.__name__,
        )
        _audit_dicom_event(
            audit_db,
            dataset,
            event_type="dicom_store_received",
            status_code=WRITE_FAILURE_STATUS,
            success=False,
            error_code="write_failed",
        )
        return WRITE_FAILURE_STATUS

    LOGGER.info(
        "C-STORE stored calling_ae=%s called_ae=%s remote_ip=%s sop_instance_uid=%s "
        "study_instance_uid=%s series_instance_uid=%s accession_number=%s modality=%s",
        _calling_ae(event),
        _called_ae(event),
        _remote_ip(event),
        _text(getattr(dataset, "SOPInstanceUID", "")),
        _text(getattr(dataset, "StudyInstanceUID", "")),
        _text(getattr(dataset, "SeriesInstanceUID", "")),
        _text(getattr(dataset, "AccessionNumber", "")),
        _text(getattr(dataset, "Modality", "")),
    )
    _audit_dicom_event(
        audit_db,
        dataset,
        event_type="dicom_store_received",
        status_code=SUCCESS_STATUS,
        success=True,
    )
    _inspect_after_store(
        dataset,
        audit_db,
        inspection_enabled=inspection_enabled,
        inspection_report_path=inspection_report_path,
    )
    forwarding_dataset, forwarding_path = _fix_after_inspection(
        dataset,
        path,
        storage_dir,
        audit_db,
        charset_fix_enabled=charset_fix_enabled,
        charset_fix_mode=charset_fix_mode,
        charset_fix_report_path=charset_fix_report_path,
    )
    if forward_mode == "queue":
        if not queue_enabled:
            LOGGER.warning(
                "DICOM queue mode rejected sop_instance_uid=%s "
                "study_instance_uid=%s accession_number=%s modality=%s mode=%s "
                "status=queue_disabled",
                _text(getattr(dataset, "SOPInstanceUID", "")),
                _text(getattr(dataset, "StudyInstanceUID", "")),
                _text(getattr(dataset, "AccessionNumber", "")),
                _text(getattr(dataset, "Modality", "")),
                forward_mode,
            )
            return WRITE_FAILURE_STATUS
        queue_id = _enqueue_after_store(
            queue_db,
            forwarding_dataset,
            forwarding_path,
            mode=forward_mode,
        )
        if queue_id is None:
            return WRITE_FAILURE_STATUS
        return SUCCESS_STATUS

    if queue_enabled and queue_db is not None:
        _enqueue_after_store(
            queue_db,
            forwarding_dataset,
            forwarding_path,
            mode=forward_mode,
        )

    if forwarder is None:
        _match_after_success(forwarding_dataset, mwl_client, audit_db)
        return SUCCESS_STATUS

    forward_result = forwarder.forward_file(forwarding_path)
    if forward_result.success:
        _audit_dicom_event(
            audit_db,
            forwarding_dataset,
            event_type="dicom_forward_success",
            status_code=forward_result.status_code,
            success=True,
        )
        _match_after_success(forwarding_dataset, mwl_client, audit_db)
        return SUCCESS_STATUS

    _audit_dicom_event(
        audit_db,
        forwarding_dataset,
        event_type="dicom_forward_failed",
        status_code=forward_result.status_code,
        success=False,
        error_code=forward_result.error,
    )
    return WRITE_FAILURE_STATUS


def _match_after_success(
    dataset: Any,
    mwl_client: MwlApiClient | None,
    audit_db: Path | None,
) -> None:
    if mwl_client is None:
        return

    try:
        response = mwl_client.get_worklist()
        match_result = match_dataset_to_worklist(dataset, response.payload)
    except MwlHttpError as error:
        LOGGER.warning(
            "DICOM MWL match skipped sop_instance_uid=%s study_instance_uid=%s "
            "accession_number=%s modality=%s error=mwl_error status=%s",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "AccessionNumber", "")),
            _text(getattr(dataset, "Modality", "")),
            error.status_code,
        )
        _audit_dicom_match(
            audit_db,
            dataset,
            MatchResult(False, None, _text(getattr(dataset, "AccessionNumber", "")) or None, None, None),
            success=False,
            error_code="mwl_error",
        )
        return
    except MwlUnavailableError:
        LOGGER.warning(
            "DICOM MWL match skipped sop_instance_uid=%s study_instance_uid=%s "
            "accession_number=%s modality=%s error=mwl_unavailable",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "AccessionNumber", "")),
            _text(getattr(dataset, "Modality", "")),
        )
        _audit_dicom_match(
            audit_db,
            dataset,
            MatchResult(False, None, _text(getattr(dataset, "AccessionNumber", "")) or None, None, None),
            success=False,
            error_code="mwl_unavailable",
        )
        return

    LOGGER.info(
        "DICOM MWL match result matched=%s matched_by=%s accession_number=%s "
        "study_instance_uid=%s sop_instance_uid=%s modality=%s",
        match_result.matched,
        match_result.matched_by or "",
        match_result.accession_number or "",
        _text(getattr(dataset, "StudyInstanceUID", "")),
        _text(getattr(dataset, "SOPInstanceUID", "")),
        _text(getattr(dataset, "Modality", "")),
    )
    _audit_dicom_match(
        audit_db,
        dataset,
        match_result,
        success=match_result.matched,
        error_code=None if match_result.matched else match_result.reason,
    )
    if match_result.matched and match_result.accession_number:
        completion_result = complete_matched_worklist(mwl_client, dataset, match_result)
        _audit_dicom_completion(
            audit_db,
            match_result,
            completion_result,
        )


def _enqueue_after_store(
    queue_db: Path | None,
    dataset: Any,
    path: Path,
    *,
    mode: str,
) -> int | None:
    if queue_db is None:
        LOGGER.warning(
            "DICOM forward queue enqueue failed sop_instance_uid=%s "
            "study_instance_uid=%s accession_number=%s modality=%s mode=%s "
            "exception=%s",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "AccessionNumber", "")),
            _text(getattr(dataset, "Modality", "")),
            mode,
            "QueueDatabaseNotConfigured",
        )
        return None
    try:
        enqueue_result = enqueue_stored_dataset(queue_db, dataset, path)
        LOGGER.info(
            "DICOM forward queue enqueue queue_id=%s sop_instance_uid=%s "
            "study_instance_uid=%s accession_number=%s modality=%s mode=%s "
            "inserted=%s status=%s",
            enqueue_result.queue_id,
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "AccessionNumber", "")),
            _text(getattr(dataset, "Modality", "")),
            mode,
            enqueue_result.inserted,
            enqueue_result.status,
        )
        return enqueue_result.queue_id
    except Exception as error:
        LOGGER.warning(
            "DICOM forward queue enqueue failed sop_instance_uid=%s "
            "study_instance_uid=%s accession_number=%s modality=%s mode=%s "
            "exception=%s",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "AccessionNumber", "")),
            _text(getattr(dataset, "Modality", "")),
            mode,
            error.__class__.__name__,
        )
        return None


def _inspect_after_store(
    dataset: Any,
    audit_db: Path | None,
    *,
    inspection_enabled: bool,
    inspection_report_path: Path | None,
) -> None:
    if not inspection_enabled:
        return
    try:
        report = inspect_dataset(dataset)
        log_inspection_report(report)
        if inspection_report_path is not None:
            append_inspection_report(inspection_report_path, report)
        _audit_dicom_inspection(
            audit_db,
            report.accession_number or None,
            success=True,
            error_code="review_required" if report.needs_charset_review else None,
        )
    except Exception as error:
        LOGGER.warning(
            "DICOM charset inspection failed sop_instance_uid=%s "
            "study_instance_uid=%s accession_number=%s modality=%s exception=%s",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "AccessionNumber", "")),
            _text(getattr(dataset, "Modality", "")),
            error.__class__.__name__,
        )
        _audit_dicom_inspection(
            audit_db,
            _text(getattr(dataset, "AccessionNumber", "")) or None,
            success=False,
            error_code="inspection_failed",
        )


def _fix_after_inspection(
    dataset: Any,
    original_path: Path,
    storage_dir: Path,
    audit_db: Path | None,
    *,
    charset_fix_enabled: bool,
    charset_fix_mode: str,
    charset_fix_report_path: Path | None,
) -> tuple[Any, Path]:
    try:
        result = maybe_fix_charset(
            dataset,
            enabled=charset_fix_enabled,
            mode=charset_fix_mode,
        )
        forwarding_path = original_path
        if result.fix_applied:
            forwarding_path = store_dataset(result.dataset, storage_dir / "forwarded")
        _record_charset_fix_result(audit_db, charset_fix_report_path, result)
        return result.dataset, forwarding_path
    except Exception as error:
        LOGGER.warning(
            "DICOM charset fix failed sop_instance_uid=%s study_instance_uid=%s "
            "accession_number=%s modality=%s exception=%s",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "AccessionNumber", "")),
            _text(getattr(dataset, "Modality", "")),
            error.__class__.__name__,
        )
        result = failure_result(
            dataset,
            enabled=charset_fix_enabled,
            mode=charset_fix_mode,
        )
        _record_charset_fix_result(audit_db, charset_fix_report_path, result)
        return dataset, original_path


def _record_charset_fix_result(
    audit_db: Path | None,
    report_path: Path | None,
    result: CharsetFixResult,
) -> None:
    try:
        log_charset_fix_result(result)
        if report_path is not None:
            append_charset_fix_report(report_path, result)
    except Exception as error:
        LOGGER.warning(
            "DICOM charset fix report write failed sop_instance_uid=%s "
            "study_instance_uid=%s accession_number=%s modality=%s exception=%s",
            result.sop_instance_uid,
            result.study_instance_uid,
            result.accession_number,
            result.modality,
            error.__class__.__name__,
        )
    _audit_dicom_charset_fix(audit_db, result)


def _audit_dicom_charset_fix(
    audit_db: Path | None,
    result: CharsetFixResult,
) -> None:
    if audit_db is None:
        return
    record_gateway_event(
        audit_db,
        event_type="dicom_charset_fix_checked",
        request_path="/dicom/c-store",
        accession_number=result.accession_number or None,
        matched_by=None,
        status_code=None,
        success=result.error_code != "charset_fix_failed",
        error_code=result.error_code,
    )


def _audit_dicom_inspection(
    audit_db: Path | None,
    accession_number: str | None,
    *,
    success: bool,
    error_code: str | None,
) -> None:
    if audit_db is None:
        return
    record_gateway_event(
        audit_db,
        event_type="dicom_charset_inspected",
        request_path="/dicom/c-store",
        accession_number=accession_number,
        matched_by=None,
        status_code=None,
        success=success,
        error_code=error_code,
    )


def _audit_dicom_completion(
    audit_db: Path | None,
    match_result: MatchResult,
    completion_result: CompletionResult,
) -> None:
    if audit_db is None:
        return
    record_gateway_event(
        audit_db,
        event_type="dicom_worklist_complete",
        request_path="/dicom/c-store",
        accession_number=match_result.accession_number,
        matched_by=match_result.matched_by,
        status_code=completion_result.status_code,
        success=completion_result.success,
        error_code=completion_result.error,
    )


def _audit_dicom_match(
    audit_db: Path | None,
    dataset: Any,
    match_result: MatchResult,
    *,
    success: bool,
    error_code: str | None,
) -> None:
    if audit_db is None:
        return
    record_gateway_event(
        audit_db,
        event_type="dicom_match",
        request_path="/dicom/c-store",
        accession_number=(
            match_result.accession_number
            or _text(getattr(dataset, "AccessionNumber", ""))
            or None
        ),
        matched_by=match_result.matched_by,
        status_code=None,
        success=success,
        error_code=error_code,
    )


def _audit_dicom_event(
    audit_db: Path | None,
    dataset: Any,
    *,
    event_type: str,
    status_code: int | None,
    success: bool,
    error_code: str | None = None,
) -> None:
    if audit_db is None:
        return
    record_gateway_event(
        audit_db,
        event_type=event_type,
        request_path="/dicom/c-store",
        accession_number=_text(getattr(dataset, "AccessionNumber", "")) or None,
        matched_by=None,
        status_code=status_code,
        success=success,
        error_code=error_code,
    )


class GatewayDicomServer:
    def __init__(
        self,
        *,
        bind: str,
        port: int,
        aet: str,
        storage_dir: Path,
        forwarder: DicomForwarder | None = None,
        mwl_client: MwlApiClient | None = None,
        audit_db: Path | None = None,
        queue_db: Path | None = None,
        queue_enabled: bool = False,
        forward_mode: str = "direct",
        inspection_enabled: bool = True,
        inspection_report_path: Path | None = None,
        charset_fix_enabled: bool = True,
        charset_fix_mode: str = "iso_ir_149_to_utf8",
        charset_fix_report_path: Path | None = None,
    ) -> None:
        self.bind = bind
        self.port = port
        self.aet = aet
        self.storage_dir = storage_dir
        self.forwarder = forwarder
        self.mwl_client = mwl_client
        self.audit_db = audit_db
        self.queue_db = queue_db
        self.queue_enabled = queue_enabled
        self.forward_mode = forward_mode
        self.inspection_enabled = inspection_enabled
        self.inspection_report_path = inspection_report_path
        self.charset_fix_enabled = charset_fix_enabled
        self.charset_fix_mode = charset_fix_mode
        self.charset_fix_report_path = charset_fix_report_path
        self._server: Any | None = None

    def start(self) -> "GatewayDicomServer":
        ae = AE(ae_title=self.aet)
        ae.require_called_aet = True
        for context in AllStoragePresentationContexts:
            ae.add_supported_context(context.abstract_syntax, TRANSFER_SYNTAXES)
        self._server = ae.start_server(
            (self.bind, self.port),
            block=False,
            evt_handlers=[(evt.EVT_C_STORE, self._handle_store)],
        )
        LOGGER.info(
            "Gateway DICOM C-STORE listening bind=%s port=%s aet=%s storage_dir=%s "
            "forward_enabled=%s queue_enabled=%s forward_mode=%s",
            self.bind,
            self.port,
            self.aet,
            self.storage_dir,
            self.forwarder is not None,
            self.queue_enabled,
            self.forward_mode,
        )
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    def _handle_store(self, event: evt.Event) -> int:
        return handle_store(
            event,
            self.storage_dir,
            forwarder=self.forwarder,
            mwl_client=self.mwl_client,
            audit_db=self.audit_db,
            queue_db=self.queue_db,
            queue_enabled=self.queue_enabled,
            forward_mode=self.forward_mode,
            inspection_enabled=self.inspection_enabled,
            inspection_report_path=self.inspection_report_path,
            charset_fix_enabled=self.charset_fix_enabled,
            charset_fix_mode=self.charset_fix_mode,
            charset_fix_report_path=self.charset_fix_report_path,
        )


def start_dicom_listener(config: GatewayConfig) -> GatewayDicomServer | None:
    if not config.gateway_dicom_enabled:
        LOGGER.info(
            "Gateway DICOM C-STORE disabled bind=%s port=%s aet=%s",
            config.gateway_dicom_bind,
            config.gateway_dicom_port,
            config.gateway_dicom_aet,
        )
        return None

    forwarder = None
    if (
        config.gateway_dicom_forward_mode == "direct"
        and config.gateway_dicom_forward_enabled
    ):
        forwarder = DicomForwarder(
            host=config.orthanc_dicom_host,
            port=config.orthanc_dicom_port,
            target_aet=config.orthanc_dicom_aet,
            calling_aet=config.gateway_forwarding_aet,
            timeout_seconds=config.gateway_dicom_forward_timeout_seconds,
        )
    mwl_client = MwlApiClient(
        config.mwl_api_url,
        config.mwl_api_timeout_seconds,
    )

    return GatewayDicomServer(
        bind=config.gateway_dicom_bind,
        port=config.gateway_dicom_port,
        aet=config.gateway_dicom_aet,
        storage_dir=config.gateway_dicom_storage_dir,
        forwarder=forwarder,
        mwl_client=mwl_client,
        audit_db=config.gateway_audit_db,
        queue_db=config.gateway_queue_db,
        queue_enabled=config.gateway_dicom_queue_enabled,
        forward_mode=config.gateway_dicom_forward_mode,
        inspection_enabled=config.gateway_dicom_inspection_enabled,
        inspection_report_path=config.gateway_dicom_inspection_report_path,
        charset_fix_enabled=config.gateway_dicom_charset_fix_enabled,
        charset_fix_mode=config.gateway_dicom_charset_fix_mode,
        charset_fix_report_path=config.gateway_dicom_charset_fix_report_path,
    ).start()
