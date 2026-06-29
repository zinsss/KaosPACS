from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MATCH_FIELDS = (
    "AccessionNumber",
    "RequestedProcedureID",
    "ScheduledProcedureStepID",
)


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    matched_by: str | None
    accession_number: str | None
    reason: str | None
    worklist_entry: dict[str, Any] | None


def match_dataset_to_worklist(dataset: Any, worklist: Any) -> MatchResult:
    entries = worklist.get("entries", []) if isinstance(worklist, dict) else []
    active_entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("Active") is True
    ]

    for field in MATCH_FIELDS:
        dataset_value = _text(getattr(dataset, field, ""))
        if not dataset_value:
            continue
        for entry in active_entries:
            if _text(entry.get(field)) == dataset_value:
                return MatchResult(
                    matched=True,
                    matched_by=field,
                    accession_number=(
                        _text(entry.get("AccessionNumber"))
                        or _text(getattr(dataset, "AccessionNumber", ""))
                        or None
                    ),
                    reason=None,
                    worklist_entry=entry,
                )

    return MatchResult(
        matched=False,
        matched_by=None,
        accession_number=_text(getattr(dataset, "AccessionNumber", "")) or None,
        reason="no_active_match",
        worklist_entry=None,
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
