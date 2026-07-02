from __future__ import annotations

import copy
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.valuerep import PersonName


LOGGER = logging.getLogger("kaospacs.gateway.dicom.charset_fix")

UTF8_CHARSET = "ISO_IR 192"
ISO_IR_149 = "ISO_IR 149"
ISO_2022_IR_149 = "ISO 2022 IR 149"
SUPPORTED_TEXT_KEYWORDS = {
    "PatientName",
    "StudyDescription",
    "SeriesDescription",
    "RequestedProcedureDescription",
    "ScheduledProcedureStepDescription",
    "InstitutionName",
    "ReferringPhysicianName",
    "PerformingPhysicianName",
}
PROTECTED_KEYWORDS = {
    "PatientID",
    "AccessionNumber",
}
SUPPORTED_VRS = {"PN", "LO", "SH", "ST", "LT", "UT"}


@dataclass(frozen=True)
class CharsetFixResult:
    dataset: Dataset
    sop_instance_uid: str
    study_instance_uid: str
    series_instance_uid: str
    accession_number: str
    modality: str
    original_specific_character_set: list[str]
    new_specific_character_set: list[str]
    fix_enabled: bool
    fix_mode: str
    fix_applied: bool
    fixed_keywords: list[str]
    skipped_keywords: list[str]
    reason: str
    error_code: str | None = None

    def to_report_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("dataset", None)
        return payload


def maybe_fix_charset(
    dataset: Dataset,
    *,
    enabled: bool,
    mode: str,
) -> CharsetFixResult:
    original_charsets = _specific_character_set(dataset)
    base = _base_result(
        dataset,
        original_specific_character_set=original_charsets,
        new_specific_character_set=original_charsets,
        fix_enabled=enabled,
        fix_mode=mode,
    )

    if not enabled:
        return _replace(base, reason="disabled", error_code="skipped_disabled")
    if mode == "off":
        return _replace(base, reason="mode_off", error_code="skipped_mode_off")
    if mode != "iso_ir_149_to_utf8":
        return _replace(base, reason="unsupported_mode", error_code="skipped_unsupported_mode")
    if not _contains_iso_ir_149(original_charsets):
        return _replace(
            base,
            reason="not_target_charset",
            error_code="skipped_not_target_charset",
        )

    working = copy.deepcopy(dataset)
    fixed_keywords: set[str] = set()
    skipped_keywords: set[str] = set()
    _rewrite_supported_text(working, fixed_keywords, skipped_keywords)
    working.SpecificCharacterSet = UTF8_CHARSET

    return CharsetFixResult(
        dataset=working,
        sop_instance_uid=_text(getattr(working, "SOPInstanceUID", "")),
        study_instance_uid=_text(getattr(working, "StudyInstanceUID", "")),
        series_instance_uid=_text(getattr(working, "SeriesInstanceUID", "")),
        accession_number=_text(getattr(working, "AccessionNumber", "")),
        modality=_text(getattr(working, "Modality", "")),
        original_specific_character_set=original_charsets,
        new_specific_character_set=[UTF8_CHARSET],
        fix_enabled=enabled,
        fix_mode=mode,
        fix_applied=True,
        fixed_keywords=sorted(fixed_keywords),
        skipped_keywords=sorted(skipped_keywords),
        reason="iso_ir_149_to_utf8_applied",
        error_code=None,
    )


def failure_result(
    dataset: Dataset,
    *,
    enabled: bool,
    mode: str,
    original_specific_character_set: list[str] | None = None,
) -> CharsetFixResult:
    charsets = original_specific_character_set or _specific_character_set(dataset)
    return CharsetFixResult(
        dataset=dataset,
        sop_instance_uid=_text(getattr(dataset, "SOPInstanceUID", "")),
        study_instance_uid=_text(getattr(dataset, "StudyInstanceUID", "")),
        series_instance_uid=_text(getattr(dataset, "SeriesInstanceUID", "")),
        accession_number=_text(getattr(dataset, "AccessionNumber", "")),
        modality=_text(getattr(dataset, "Modality", "")),
        original_specific_character_set=charsets,
        new_specific_character_set=charsets,
        fix_enabled=enabled,
        fix_mode=mode,
        fix_applied=False,
        fixed_keywords=[],
        skipped_keywords=[],
        reason="charset_fix_failed",
        error_code="charset_fix_failed",
    )


def append_charset_fix_report(path: Path, result: CharsetFixResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(result.to_report_dict(), ensure_ascii=False, sort_keys=True)
            + "\n"
        )


def log_charset_fix_result(result: CharsetFixResult) -> None:
    LOGGER.info(
        "DICOM charset fix checked sop_instance_uid=%s study_instance_uid=%s "
        "series_instance_uid=%s accession_number=%s modality=%s "
        "original_specific_character_set=%s new_specific_character_set=%s "
        "fix_enabled=%s fix_mode=%s fix_applied=%s fixed_keywords=%s "
        "skipped_keywords=%s reason=%s error_code=%s",
        result.sop_instance_uid,
        result.study_instance_uid,
        result.series_instance_uid,
        result.accession_number,
        result.modality,
        result.original_specific_character_set,
        result.new_specific_character_set,
        result.fix_enabled,
        result.fix_mode,
        result.fix_applied,
        result.fixed_keywords,
        result.skipped_keywords,
        result.reason,
        result.error_code or "",
    )


def _rewrite_supported_text(
    dataset: Dataset,
    fixed_keywords: set[str],
    skipped_keywords: set[str],
) -> None:
    for element in dataset:
        if element.VR == "SQ":
            for item in element.value:
                if isinstance(item, Dataset):
                    _rewrite_supported_text(item, fixed_keywords, skipped_keywords)
            continue

        keyword = element.keyword
        if element.tag.is_private and str(element.VR) in SUPPORTED_VRS:
            skipped_keywords.add(keyword or str(element.tag))
            continue
        if keyword in PROTECTED_KEYWORDS:
            skipped_keywords.add(keyword)
            continue
        if keyword not in SUPPORTED_TEXT_KEYWORDS:
            continue
        if str(element.VR) not in SUPPORTED_VRS:
            skipped_keywords.add(keyword)
            continue
        element.value = _normalize_text_value(element.value)
        fixed_keywords.add(keyword)


def _normalize_text_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("euc_kr")
    if isinstance(value, str):
        return value
    if isinstance(value, PersonName):
        return value
    if isinstance(value, MultiValue):
        return MultiValue(
            value.type_constructor,
            [_normalize_text_value(item) for item in value],
        )
    if isinstance(value, list):
        return [_normalize_text_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_text_value(item) for item in value)
    return value


def _base_result(
    dataset: Dataset,
    *,
    original_specific_character_set: list[str],
    new_specific_character_set: list[str],
    fix_enabled: bool,
    fix_mode: str,
) -> CharsetFixResult:
    skipped_keywords = sorted(
        keyword
        for keyword in PROTECTED_KEYWORDS
        if getattr(dataset, keyword, None) not in (None, "")
    )
    return CharsetFixResult(
        dataset=dataset,
        sop_instance_uid=_text(getattr(dataset, "SOPInstanceUID", "")),
        study_instance_uid=_text(getattr(dataset, "StudyInstanceUID", "")),
        series_instance_uid=_text(getattr(dataset, "SeriesInstanceUID", "")),
        accession_number=_text(getattr(dataset, "AccessionNumber", "")),
        modality=_text(getattr(dataset, "Modality", "")),
        original_specific_character_set=original_specific_character_set,
        new_specific_character_set=new_specific_character_set,
        fix_enabled=fix_enabled,
        fix_mode=fix_mode,
        fix_applied=False,
        fixed_keywords=[],
        skipped_keywords=skipped_keywords,
        reason="",
        error_code=None,
    )


def _replace(
    result: CharsetFixResult,
    *,
    reason: str,
    error_code: str | None,
) -> CharsetFixResult:
    return CharsetFixResult(
        dataset=result.dataset,
        sop_instance_uid=result.sop_instance_uid,
        study_instance_uid=result.study_instance_uid,
        series_instance_uid=result.series_instance_uid,
        accession_number=result.accession_number,
        modality=result.modality,
        original_specific_character_set=result.original_specific_character_set,
        new_specific_character_set=result.new_specific_character_set,
        fix_enabled=result.fix_enabled,
        fix_mode=result.fix_mode,
        fix_applied=False,
        fixed_keywords=result.fixed_keywords,
        skipped_keywords=result.skipped_keywords,
        reason=reason,
        error_code=error_code,
    )


def _specific_character_set(dataset: Dataset) -> list[str]:
    raw = getattr(dataset, "SpecificCharacterSet", None)
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        return [_text(raw)]
    try:
        return [_text(item) for item in raw if _text(item)]
    except TypeError:
        return [_text(raw)]


def _contains_iso_ir_149(charsets: list[str]) -> bool:
    normalized = {charset.strip().upper() for charset in charsets}
    return ISO_IR_149 in normalized or ISO_2022_IR_149 in normalized


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
