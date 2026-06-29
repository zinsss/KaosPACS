from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydicom.uid import ExplicitVRBigEndian, ExplicitVRLittleEndian, ImplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.presentation import AllStoragePresentationContexts

from app.config import GatewayConfig
from app.dicom.forwarder import DicomForwarder
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
    audit_db: Path | None = None,
) -> int:
    dataset = event.dataset
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
        "study_instance_uid=%s accession_number=%s modality=%s",
        _calling_ae(event),
        _called_ae(event),
        _remote_ip(event),
        _text(getattr(dataset, "SOPInstanceUID", "")),
        _text(getattr(dataset, "StudyInstanceUID", "")),
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

    if forwarder is None:
        return SUCCESS_STATUS

    forward_result = forwarder.forward_file(path)
    if forward_result.success:
        _audit_dicom_event(
            audit_db,
            dataset,
            event_type="dicom_forward_success",
            status_code=forward_result.status_code,
            success=True,
        )
        return SUCCESS_STATUS

    _audit_dicom_event(
        audit_db,
        dataset,
        event_type="dicom_forward_failed",
        status_code=forward_result.status_code,
        success=False,
        error_code=forward_result.error,
    )
    return WRITE_FAILURE_STATUS


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
        audit_db: Path | None = None,
    ) -> None:
        self.bind = bind
        self.port = port
        self.aet = aet
        self.storage_dir = storage_dir
        self.forwarder = forwarder
        self.audit_db = audit_db
        self._server: Any | None = None

    def start(self) -> "GatewayDicomServer":
        ae = AE(ae_title=self.aet)
        for context in AllStoragePresentationContexts:
            ae.add_supported_context(context.abstract_syntax, TRANSFER_SYNTAXES)
        self._server = ae.start_server(
            (self.bind, self.port),
            block=False,
            evt_handlers=[(evt.EVT_C_STORE, self._handle_store)],
        )
        LOGGER.info(
            "Gateway DICOM C-STORE skeleton listening bind=%s port=%s aet=%s storage_dir=%s "
            "forward_enabled=%s",
            self.bind,
            self.port,
            self.aet,
            self.storage_dir,
            self.forwarder is not None,
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
            audit_db=self.audit_db,
        )


def start_dicom_listener(config: GatewayConfig) -> GatewayDicomServer | None:
    if not config.gateway_dicom_enabled:
        LOGGER.info(
            "Gateway DICOM C-STORE skeleton disabled bind=%s port=%s aet=%s",
            config.gateway_dicom_bind,
            config.gateway_dicom_port,
            config.gateway_dicom_aet,
        )
        return None

    forwarder = None
    if config.gateway_dicom_forward_enabled:
        forwarder = DicomForwarder(
            host=config.orthanc_dicom_host,
            port=config.orthanc_dicom_port,
            target_aet=config.orthanc_dicom_aet,
            calling_aet=config.gateway_forwarding_aet,
            timeout_seconds=config.gateway_dicom_forward_timeout_seconds,
        )

    return GatewayDicomServer(
        bind=config.gateway_dicom_bind,
        port=config.gateway_dicom_port,
        aet=config.gateway_dicom_aet,
        storage_dir=config.gateway_dicom_storage_dir,
        forwarder=forwarder,
        audit_db=config.gateway_audit_db,
    ).start()
