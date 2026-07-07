from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("kaospacs.gateway.operational_metadata")

TABLE = "operational_modality_metadata"


@dataclass(frozen=True)
class OperationalMetadataRecord:
    accession_number: str
    orthanc_study_id: str
    study_instance_uid: str
    sop_instance_uid: str
    patient_id: str
    dicom_modality_original: str
    workflow_modality: str
    station_aet: str
    study_type: str
    display_modality: str
    aio_domain_candidate: str
    matched_by: str
    match_confidence: str
    source: str
    created_at: str
    updated_at: str

    def to_payload(self) -> dict[str, Any]:
        record = asdict(self)
        return {
            "metadata": record,
            "mapping_evidence": {
                "dicom_modality_original": self.dicom_modality_original,
                "workflow_modality": self.workflow_modality,
                "station_aet": self.station_aet,
                "study_type": self.study_type,
                "matched_by": self.matched_by,
                "match_confidence": self.match_confidence,
                "source": self.source,
            },
        }


def init_operational_metadata_db(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            _ensure_schema(connection)
    except Exception as error:
        LOGGER.warning(
            "Gateway operational metadata DB initialization failed path=%s error=%s",
            path,
            error.__class__.__name__,
        )


def save_operational_metadata(
    path: Path,
    *,
    dataset: Any,
    worklist_entry: dict[str, Any],
    matched_by: str | None,
    orthanc_study_id: str | None = None,
) -> OperationalMetadataRecord | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            _ensure_schema(connection)
            record = build_operational_metadata_record(
                dataset=dataset,
                worklist_entry=worklist_entry,
                matched_by=matched_by,
                orthanc_study_id=orthanc_study_id,
                existing_created_at=_existing_created_at(connection, dataset),
            )
            connection.execute(
                f"""
                INSERT INTO {TABLE} (
                    accession_number,
                    orthanc_study_id,
                    study_instance_uid,
                    sop_instance_uid,
                    patient_id,
                    dicom_modality_original,
                    workflow_modality,
                    station_aet,
                    study_type,
                    display_modality,
                    aio_domain_candidate,
                    matched_by,
                    match_confidence,
                    source,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sop_instance_uid) DO UPDATE SET
                    accession_number=excluded.accession_number,
                    orthanc_study_id=excluded.orthanc_study_id,
                    study_instance_uid=excluded.study_instance_uid,
                    patient_id=excluded.patient_id,
                    dicom_modality_original=excluded.dicom_modality_original,
                    workflow_modality=excluded.workflow_modality,
                    station_aet=excluded.station_aet,
                    study_type=excluded.study_type,
                    display_modality=excluded.display_modality,
                    aio_domain_candidate=excluded.aio_domain_candidate,
                    matched_by=excluded.matched_by,
                    match_confidence=excluded.match_confidence,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                _record_values(record),
            )
        LOGGER.info(
            "Gateway operational metadata saved accession_number=%s sop_instance_uid=%s "
            "study_instance_uid=%s dicom_modality_original=%s workflow_modality=%s "
            "station_aet=%s study_type=%s display_modality=%s aio_domain_candidate=%s "
            "matched_by=%s match_confidence=%s source=%s",
            record.accession_number,
            record.sop_instance_uid,
            record.study_instance_uid,
            record.dicom_modality_original,
            record.workflow_modality,
            record.station_aet,
            record.study_type,
            record.display_modality,
            record.aio_domain_candidate,
            record.matched_by,
            record.match_confidence,
            record.source,
        )
        return record
    except Exception as error:
        LOGGER.warning(
            "Gateway operational metadata save failed accession_number=%s "
            "sop_instance_uid=%s study_instance_uid=%s exception=%s",
            _text(getattr(dataset, "AccessionNumber", "")),
            _text(getattr(dataset, "SOPInstanceUID", "")),
            _text(getattr(dataset, "StudyInstanceUID", "")),
            error.__class__.__name__,
        )
        return None


def build_operational_metadata_record(
    *,
    dataset: Any,
    worklist_entry: dict[str, Any],
    matched_by: str | None,
    orthanc_study_id: str | None = None,
    existing_created_at: str | None = None,
) -> OperationalMetadataRecord:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    workflow_modality = _text(worklist_entry.get("Modality"))
    station_aet = (
        _text(worklist_entry.get("ScheduledStationAETitle"))
        or _text(worklist_entry.get("StationAET"))
    )
    study_type = _text(worklist_entry.get("StudyType"))
    display_modality, aio_domain_candidate = derive_display_modality(
        station_aet=station_aet,
        workflow_modality=workflow_modality,
        study_type=study_type,
    )
    return OperationalMetadataRecord(
        accession_number=(
            _text(worklist_entry.get("AccessionNumber"))
            or _text(getattr(dataset, "AccessionNumber", ""))
        ),
        orthanc_study_id=_text(orthanc_study_id),
        study_instance_uid=_text(getattr(dataset, "StudyInstanceUID", "")),
        sop_instance_uid=_text(getattr(dataset, "SOPInstanceUID", "")),
        patient_id=_text(worklist_entry.get("PatientID")),
        dicom_modality_original=_text(getattr(dataset, "Modality", "")),
        workflow_modality=workflow_modality,
        station_aet=station_aet,
        study_type=study_type,
        display_modality=display_modality,
        aio_domain_candidate=aio_domain_candidate,
        matched_by=_text(matched_by),
        match_confidence="exact" if matched_by else "",
        source="gateway_mwl_match",
        created_at=existing_created_at or now,
        updated_at=now,
    )


def derive_display_modality(
    *,
    station_aet: str,
    workflow_modality: str,
    study_type: str = "",
) -> tuple[str, str]:
    station = station_aet.strip().upper()
    workflow = workflow_modality.strip().upper()
    study = study_type.strip().upper()
    values = {station, workflow, study}
    if "INNOVISION" in values or "CR" in values:
        return "X-ray", "cxr"
    if "BMD" in values:
        return "BMD", "bmd"
    if "ECG" in values:
        return "ECG", "ecg"
    return "Unknown", "unsupported"


def get_by_orthanc_study_id(path: Path, orthanc_study_id: str) -> OperationalMetadataRecord | None:
    return _get_one(
        path,
        "orthanc_study_id = ?",
        (_text(orthanc_study_id),),
    )


def get_by_accession_number(path: Path, accession_number: str) -> OperationalMetadataRecord | None:
    return _get_one(
        path,
        "accession_number = ?",
        (_text(accession_number),),
    )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY,
            accession_number TEXT NOT NULL,
            orthanc_study_id TEXT,
            study_instance_uid TEXT,
            sop_instance_uid TEXT NOT NULL,
            patient_id TEXT,
            dicom_modality_original TEXT,
            workflow_modality TEXT,
            station_aet TEXT,
            study_type TEXT,
            display_modality TEXT NOT NULL,
            aio_domain_candidate TEXT NOT NULL,
            matched_by TEXT,
            match_confidence TEXT,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE}_sop_instance_uid "
        f"ON {TABLE}(sop_instance_uid)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_orthanc_study_id "
        f"ON {TABLE}(orthanc_study_id)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_accession_number "
        f"ON {TABLE}(accession_number)"
    )


def _existing_created_at(connection: sqlite3.Connection, dataset: Any) -> str | None:
    sop_instance_uid = _text(getattr(dataset, "SOPInstanceUID", ""))
    if not sop_instance_uid:
        return None
    row = connection.execute(
        f"SELECT created_at FROM {TABLE} WHERE sop_instance_uid = ?",
        (sop_instance_uid,),
    ).fetchone()
    return str(row[0]) if row else None


def _get_one(
    path: Path,
    where_clause: str,
    params: tuple[str, ...],
) -> OperationalMetadataRecord | None:
    try:
        with sqlite3.connect(path) as connection:
            _ensure_schema(connection)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                f"""
                SELECT
                    accession_number,
                    orthanc_study_id,
                    study_instance_uid,
                    sop_instance_uid,
                    patient_id,
                    dicom_modality_original,
                    workflow_modality,
                    station_aet,
                    study_type,
                    display_modality,
                    aio_domain_candidate,
                    matched_by,
                    match_confidence,
                    source,
                    created_at,
                    updated_at
                FROM {TABLE}
                WHERE {where_clause}
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
    except Exception as error:
        LOGGER.warning(
            "Gateway operational metadata lookup failed exception=%s",
            error.__class__.__name__,
        )
        return None
    return _row_to_record(row) if row else None


def _record_values(record: OperationalMetadataRecord) -> tuple[str, ...]:
    return (
        record.accession_number,
        record.orthanc_study_id,
        record.study_instance_uid,
        record.sop_instance_uid,
        record.patient_id,
        record.dicom_modality_original,
        record.workflow_modality,
        record.station_aet,
        record.study_type,
        record.display_modality,
        record.aio_domain_candidate,
        record.matched_by,
        record.match_confidence,
        record.source,
        record.created_at,
        record.updated_at,
    )


def _row_to_record(row: sqlite3.Row) -> OperationalMetadataRecord:
    return OperationalMetadataRecord(
        accession_number=str(row["accession_number"] or ""),
        orthanc_study_id=str(row["orthanc_study_id"] or ""),
        study_instance_uid=str(row["study_instance_uid"] or ""),
        sop_instance_uid=str(row["sop_instance_uid"] or ""),
        patient_id=str(row["patient_id"] or ""),
        dicom_modality_original=str(row["dicom_modality_original"] or ""),
        workflow_modality=str(row["workflow_modality"] or ""),
        station_aet=str(row["station_aet"] or ""),
        study_type=str(row["study_type"] or ""),
        display_modality=str(row["display_modality"] or ""),
        aio_domain_candidate=str(row["aio_domain_candidate"] or ""),
        matched_by=str(row["matched_by"] or ""),
        match_confidence=str(row["match_confidence"] or ""),
        source=str(row["source"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
