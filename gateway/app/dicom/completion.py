from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.clients.mwl import MwlApiClient, MwlHttpError, MwlUnavailableError
from app.dicom.matcher import MatchResult


LOGGER = logging.getLogger("kaospacs.gateway.dicom.completion")


@dataclass(frozen=True)
class CompletionResult:
    success: bool
    status_code: int | None
    error: str | None = None


def complete_matched_worklist(
    mwl_client: MwlApiClient,
    dataset: Any,
    match_result: MatchResult,
) -> CompletionResult:
    accession_number = _text(match_result.accession_number)
    if not match_result.matched or not accession_number:
        return CompletionResult(False, None, "not_eligible")

    try:
        response = mwl_client.complete_worklist({"AccessionNumber": accession_number})
    except MwlHttpError as error:
        LOGGER.warning(
            "DICOM worklist completion failed accession_number=%s matched_by=%s "
            "sop_instance_uid=%s study_instance_uid=%s modality=%s status=%s",
            accession_number,
            match_result.matched_by or "",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "Modality", "")),
            error.status_code,
        )
        return CompletionResult(False, error.status_code, "mwl_error")
    except MwlUnavailableError:
        LOGGER.warning(
            "DICOM worklist completion unavailable accession_number=%s matched_by=%s "
            "sop_instance_uid=%s study_instance_uid=%s modality=%s",
            accession_number,
            match_result.matched_by or "",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "Modality", "")),
        )
        return CompletionResult(False, None, "mwl_unavailable")
    except Exception as error:
        LOGGER.warning(
            "DICOM worklist completion failed accession_number=%s matched_by=%s "
            "sop_instance_uid=%s study_instance_uid=%s modality=%s exception=%s",
            accession_number,
            match_result.matched_by or "",
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            _text(getattr(dataset, "Modality", "")),
            error.__class__.__name__,
        )
        return CompletionResult(False, None, "completion_failed")

    success = response.status_code < 400
    LOGGER.info(
        "DICOM worklist completion result accession_number=%s matched_by=%s "
        "sop_instance_uid=%s study_instance_uid=%s modality=%s status=%s success=%s",
        accession_number,
        match_result.matched_by or "",
        _text(getattr(dataset, "SOPInstanceUID", "")),
        _text(getattr(dataset, "StudyInstanceUID", "")),
        _text(getattr(dataset, "Modality", "")),
        response.status_code,
        success,
    )
    return CompletionResult(
        success,
        response.status_code,
        None if success else "mwl_error",
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
