from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydicom import dcmread
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE


LOGGER = logging.getLogger("kaospacs.gateway.dicom.forwarder")
SUCCESS_STATUS = 0x0000


@dataclass(frozen=True)
class ForwardResult:
    success: bool
    status_code: int | None
    error: str | None = None


@dataclass(frozen=True)
class DicomForwarder:
    host: str
    port: int
    target_aet: str
    calling_aet: str
    timeout_seconds: float

    def forward_file(self, path: Path) -> ForwardResult:
        try:
            dataset = dcmread(path, force=True)
        except Exception as error:
            LOGGER.error(
                "DICOM forward read failed exception=%s target_host=%s target_port=%s target_aet=%s",
                error.__class__.__name__,
                self.host,
                self.port,
                self.target_aet,
            )
            return ForwardResult(False, None, "read_failed")

        return self.forward_dataset(dataset)

    def forward_dataset(self, dataset: Any) -> ForwardResult:
        ae = AE(ae_title=self.calling_aet)
        ae.acse_timeout = self.timeout_seconds
        ae.dimse_timeout = self.timeout_seconds
        ae.network_timeout = self.timeout_seconds

        sop_class_uid = getattr(dataset, "SOPClassUID", None)
        if not sop_class_uid:
            LOGGER.error(
                "DICOM forward failed sop_instance_uid=%s target_host=%s target_port=%s "
                "target_aet=%s error=missing_sop_class_uid",
                _text(getattr(dataset, "SOPInstanceUID", "")),
                self.host,
                self.port,
                self.target_aet,
            )
            return ForwardResult(False, None, "missing_sop_class_uid")

        transfer_syntax = getattr(getattr(dataset, "file_meta", None), "TransferSyntaxUID", None)
        ae.add_requested_context(sop_class_uid, transfer_syntax or ExplicitVRLittleEndian)

        association = None
        try:
            association = ae.associate(self.host, self.port, ae_title=self.target_aet)
            if not association.is_established:
                LOGGER.error(
                    "DICOM forward association failed sop_instance_uid=%s study_instance_uid=%s "
                    "accession_number=%s modality=%s target_host=%s target_port=%s target_aet=%s",
                    _text(getattr(dataset, "SOPInstanceUID", "")),
                    _text(getattr(dataset, "StudyInstanceUID", "")),
                    _text(getattr(dataset, "AccessionNumber", "")),
                    _text(getattr(dataset, "Modality", "")),
                    self.host,
                    self.port,
                    self.target_aet,
                )
                return ForwardResult(False, None, "association_failed")

            status = association.send_c_store(dataset)
            status_code = int(getattr(status, "Status", 0xC000))
            success = status_code == SUCCESS_STATUS
            log = LOGGER.info if success else LOGGER.error
            log(
                "DICOM forward result sop_instance_uid=%s study_instance_uid=%s accession_number=%s "
                "modality=%s target_host=%s target_port=%s target_aet=%s status=%s success=%s",
                _text(getattr(dataset, "SOPInstanceUID", "")),
                _text(getattr(dataset, "StudyInstanceUID", "")),
                _text(getattr(dataset, "AccessionNumber", "")),
                _text(getattr(dataset, "Modality", "")),
                self.host,
                self.port,
                self.target_aet,
                status_code,
                success,
            )
            return ForwardResult(success, status_code, None if success else "c_store_failed")
        except (OSError, TimeoutError, socket.timeout) as error:
            LOGGER.error(
                "DICOM forward unavailable sop_instance_uid=%s target_host=%s target_port=%s "
                "target_aet=%s exception=%s",
                _text(getattr(dataset, "SOPInstanceUID", "")),
                self.host,
                self.port,
                self.target_aet,
                error.__class__.__name__,
            )
            return ForwardResult(False, None, "forward_unavailable")
        except Exception as error:
            LOGGER.error(
                "DICOM forward failed sop_instance_uid=%s target_host=%s target_port=%s "
                "target_aet=%s exception=%s",
                _text(getattr(dataset, "SOPInstanceUID", "")),
                self.host,
                self.port,
                self.target_aet,
                error.__class__.__name__,
            )
            return ForwardResult(False, None, "forward_failed")
        finally:
            if association is not None and association.is_established:
                association.release()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
