from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRBigEndian, ExplicitVRLittleEndian, ImplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.presentation import AllStoragePresentationContexts

from .config import GatewayConfig


LOGGER = logging.getLogger("kaospacs.gateway.dicom")
SUCCESS_STATUS = 0x0000
WRITE_FAILURE_STATUS = 0xA700
TRANSFER_SYNTAXES = [
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    ExplicitVRBigEndian,
]
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_dicom_filename(dataset: Dataset) -> str:
    raw_value = str(getattr(dataset, "SOPInstanceUID", "") or uuid.uuid4())
    safe_value = SAFE_FILENAME_PATTERN.sub("_", raw_value).strip("._")
    if not safe_value:
        safe_value = str(uuid.uuid4())
    return f"{safe_value}.dcm"


def dicom_storage_path(storage_dir: Path, dataset: Dataset) -> Path:
    return storage_dir / safe_dicom_filename(dataset)


def store_dataset(dataset: Dataset, storage_dir: Path) -> Path:
    storage_dir.mkdir(parents=True, exist_ok=True)
    path = dicom_storage_path(storage_dir, dataset)
    dataset.save_as(path, write_like_original=False)
    return path


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


def handle_store(event: evt.Event, storage_dir: Path) -> int:
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
        return WRITE_FAILURE_STATUS

    LOGGER.info(
        "C-STORE stored calling_ae=%s called_ae=%s remote_ip=%s sop_instance_uid=%s "
        "study_instance_uid=%s accession_number=%s modality=%s path=%s",
        _calling_ae(event),
        _called_ae(event),
        _remote_ip(event),
        _text(getattr(dataset, "SOPInstanceUID", "")),
        _text(getattr(dataset, "StudyInstanceUID", "")),
        _text(getattr(dataset, "AccessionNumber", "")),
        _text(getattr(dataset, "Modality", "")),
        path,
    )
    return SUCCESS_STATUS


class GatewayDicomServer:
    def __init__(self, *, bind: str, port: int, aet: str, storage_dir: Path) -> None:
        self.bind = bind
        self.port = port
        self.aet = aet
        self.storage_dir = storage_dir
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
            "Gateway DICOM C-STORE skeleton listening bind=%s port=%s aet=%s storage_dir=%s",
            self.bind,
            self.port,
            self.aet,
            self.storage_dir,
        )
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    def _handle_store(self, event: evt.Event) -> int:
        return handle_store(event, self.storage_dir)


def start_dicom_listener(config: GatewayConfig) -> GatewayDicomServer | None:
    if not config.gateway_dicom_enabled:
        LOGGER.info(
            "Gateway DICOM C-STORE skeleton disabled bind=%s port=%s aet=%s",
            config.gateway_dicom_bind,
            config.gateway_dicom_port,
            config.gateway_dicom_aet,
        )
        return None

    return GatewayDicomServer(
        bind=config.gateway_dicom_bind,
        port=config.gateway_dicom_port,
        aet=config.gateway_dicom_aet,
        storage_dir=config.gateway_dicom_storage_dir,
    ).start()
