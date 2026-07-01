from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydicom.datadict import keyword_for_tag


LOGGER = logging.getLogger("kaospacs.gateway.dicom.inspection")

TEXT_VRS = {"PN", "LO", "SH", "ST", "LT", "UT"}
KNOWN_TEXT_KEYWORDS = (
    "PatientName",
    "PatientID",
    "StudyDescription",
    "SeriesDescription",
    "RequestedProcedureDescription",
    "ScheduledProcedureStepDescription",
    "InstitutionName",
    "ReferringPhysicianName",
    "PerformingPhysicianName",
)
KNOWN_CHARSETS = {
    "",
    "ISO_IR 6",
    "ISO_IR 100",
    "ISO_IR 101",
    "ISO_IR 109",
    "ISO_IR 110",
    "ISO_IR 126",
    "ISO_IR 127",
    "ISO_IR 138",
    "ISO_IR 144",
    "ISO_IR 148",
    "ISO_IR 149",
    "ISO_IR 192",
    "GB18030",
    "GBK",
}
KOREAN_CHARSET_MARKERS = {
    "ISO_IR 149",
    "EUC-KR",
    "EUC_KR",
    "KS_X_1001",
    "KS X 1001",
    "KOREAN",
}


@dataclass(frozen=True)
class TextTagInfo:
    keyword: str
    tag: str
    vr: str


@dataclass(frozen=True)
class DicomInspectionReport:
    sop_class_uid: str
    sop_instance_uid: str
    study_instance_uid: str
    series_instance_uid: str
    accession_number: str
    modality: str
    specific_character_set: list[str]
    transfer_syntax_uid: str
    text_tag_presence: dict[str, bool]
    text_vr_counts: dict[str, int]
    additional_text_tags: list[TextTagInfo]
    needs_charset_review: bool
    review_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_dataset(dataset: Any) -> DicomInspectionReport:
    charsets = _specific_character_set(dataset)
    text_presence = {keyword: False for keyword in KNOWN_TEXT_KEYWORDS}
    vr_counts = {vr: 0 for vr in sorted(TEXT_VRS)}
    additional_text_tags: list[TextTagInfo] = []

    for element in dataset.iterall():
        vr = str(element.VR)
        if vr not in TEXT_VRS:
            continue
        vr_counts[vr] += 1
        keyword = element.keyword or keyword_for_tag(element.tag) or str(element.tag)
        if keyword in KNOWN_TEXT_KEYWORDS:
            text_presence[keyword] = element.value not in (None, "")
        else:
            additional_text_tags.append(
                TextTagInfo(keyword=keyword, tag=str(element.tag), vr=vr)
            )

    review_reasons = _review_reasons(charsets, text_presence, vr_counts)
    return DicomInspectionReport(
        sop_class_uid=_text(getattr(dataset, "SOPClassUID", "")),
        sop_instance_uid=_text(getattr(dataset, "SOPInstanceUID", "")),
        study_instance_uid=_text(getattr(dataset, "StudyInstanceUID", "")),
        series_instance_uid=_text(getattr(dataset, "SeriesInstanceUID", "")),
        accession_number=_text(getattr(dataset, "AccessionNumber", "")),
        modality=_text(getattr(dataset, "Modality", "")),
        specific_character_set=charsets,
        transfer_syntax_uid=_transfer_syntax_uid(dataset),
        text_tag_presence=text_presence,
        text_vr_counts=vr_counts,
        additional_text_tags=additional_text_tags,
        needs_charset_review=bool(review_reasons),
        review_reasons=review_reasons,
    )


def append_inspection_report(path: Path, report: DicomInspectionReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
            + "\n"
        )


def log_inspection_report(report: DicomInspectionReport) -> None:
    LOGGER.info(
        "DICOM charset inspection sop_instance_uid=%s study_instance_uid=%s "
        "series_instance_uid=%s accession_number=%s modality=%s "
        "specific_character_set=%s transfer_syntax_uid=%s text_tags_present=%s "
        "text_vr_counts=%s needs_charset_review=%s review_reasons=%s",
        report.sop_instance_uid,
        report.study_instance_uid,
        report.series_instance_uid,
        report.accession_number,
        report.modality,
        report.specific_character_set,
        report.transfer_syntax_uid,
        sorted(
            keyword
            for keyword, present in report.text_tag_presence.items()
            if present
        ),
        report.text_vr_counts,
        report.needs_charset_review,
        report.review_reasons,
    )


def _specific_character_set(dataset: Any) -> list[str]:
    raw = getattr(dataset, "SpecificCharacterSet", None)
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        return [_text(raw)]
    try:
        return [_text(item) for item in raw if _text(item)]
    except TypeError:
        return [_text(raw)]


def _transfer_syntax_uid(dataset: Any) -> str:
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is None:
        return ""
    return _text(getattr(file_meta, "TransferSyntaxUID", ""))


def _review_reasons(
    charsets: list[str],
    text_presence: dict[str, bool],
    vr_counts: dict[str, int],
) -> list[str]:
    reasons: list[str] = []
    has_text = any(text_presence.values()) or any(vr_counts.values())
    if not charsets and has_text:
        reasons.append("missing_specific_character_set_with_text_tags")
        reasons.append("korean_text_possible")
        return reasons

    normalized = [_normalize_charset(charset) for charset in charsets]
    for raw, charset in zip(charsets, normalized):
        if charset == "ISO_IR 149":
            reasons.append("specific_character_set_iso_ir_149")
            reasons.append("korean_text_possible")
        elif any(marker in charset for marker in KOREAN_CHARSET_MARKERS):
            reasons.append("korean_charset_declared")
            reasons.append("korean_text_possible")
        elif raw and charset not in KNOWN_CHARSETS:
            reasons.append("unsupported_specific_character_set")

    return _deduplicate(reasons)


def _normalize_charset(charset: str) -> str:
    return charset.strip().upper().replace("\\", "")


def _deduplicate(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
