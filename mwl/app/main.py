from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pynetdicom import AE, evt
from pynetdicom.sop_class import ModalityWorklistInformationFind, Verification


DEFAULT_AE_TITLE = "VIEWREX_WL"
DEFAULT_PORT = 105
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8055
DEFAULT_WORKLIST_PATH = Path("/app/config/worklist.json")
DEFAULT_AUDIT_DB_PATH = Path("/app/data/mwl_audit.sqlite3")
REQUIRED_FIELDS = (
    "PatientID",
    "PatientName",
    "AccessionNumber",
    "Modality",
    "ScheduledStationAETitle",
    "ScheduledProcedureStepDescription",
)

LOGGER = logging.getLogger("kaospacs.mwl")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _entry_summary(entry: dict[str, Any]) -> str:
    return (
        f"accession={_text(entry.get('AccessionNumber'))!r} "
        f"chart_no={_text(entry.get('PatientID'))!r} "
        f"modality={_text(entry.get('Modality'))!r} "
        f"station_aet={_text(entry.get('ScheduledStationAETitle'))!r}"
    )


def _item_summary(dataset: Dataset) -> str:
    step = dataset.ScheduledProcedureStepSequence[0]
    return (
        f"patient_id={_dataset_value(dataset, 'PatientID')!r} "
        f"accession={_dataset_value(dataset, 'AccessionNumber')!r} "
        f"modality={_dataset_value(step, 'Modality')!r} "
        f"station_aet={_dataset_value(step, 'ScheduledStationAETitle')!r}"
    )


def _parse_expires_at(value: Any) -> datetime:
    raw = _text(value)
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    return datetime.fromisoformat(raw)


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _scheduled_at(entry: dict[str, Any]) -> str:
    date = _text(entry.get("ScheduledProcedureStepStartDate"))
    time = _text(entry.get("ScheduledProcedureStepStartTime"))
    if not date:
        return ""
    if len(date) == 8:
        formatted_date = f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
    else:
        formatted_date = date
    if not time:
        return formatted_date
    if len(time) >= 6:
        formatted_time = f"{time[0:2]}:{time[2:4]}:{time[4:6]}"
    else:
        formatted_time = time
    return f"{formatted_date}T{formatted_time}"


def _is_expired(expires_at: datetime, now: datetime) -> bool:
    if expires_at.tzinfo is None:
        compare_now = now.replace(tzinfo=None)
    else:
        compare_now = now if now.tzinfo is not None else now.astimezone()
    return expires_at <= compare_now


def _entry_status(entry: dict[str, Any], now: datetime | None = None) -> str:
    if _text(entry.get("CancelledAt")):
        return "cancelled"
    if _text(entry.get("CompletedAt")):
        return "completed"
    if not entry.get("Active", True):
        return "inactive"

    expires_at = _text(entry.get("ExpiresAt"))
    if expires_at:
        effective_now = now or datetime.now().astimezone()
        try:
            if _is_expired(_parse_expires_at(expires_at), effective_now):
                return "expired"
        except ValueError:
            return "invalid"
    return "active"


def validate_worklist_entry(entry: Any, index: int) -> list[str]:
    if not isinstance(entry, dict):
        return [f"entry {index}: must be an object"]

    errors = []
    active = entry.get("Active", True)
    if not isinstance(active, bool):
        errors.append(f"entry {index}: Active must be true or false")

    missing = [field for field in REQUIRED_FIELDS if not _text(entry.get(field))]
    if missing:
        errors.append(
            f"entry {index}: missing required fields {','.join(missing)}"
        )

    expires_at = _text(entry.get("ExpiresAt"))
    if expires_at:
        try:
            _parse_expires_at(expires_at)
        except ValueError:
            errors.append(f"entry {index}: invalid ExpiresAt {expires_at!r}")

    return errors


def validate_worklist_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload must be an object"]

    entries = payload.get("entries")
    if not isinstance(entries, list):
        return ["payload.entries must be a list"]

    errors = []
    for index, entry in enumerate(entries):
        errors.extend(validate_worklist_entry(entry, index))
    return errors


def _is_returnable_entry(entry: dict[str, Any], index: int, now: datetime) -> bool:
    errors = validate_worklist_entry(entry, index)
    if errors:
        for error in errors:
            LOGGER.warning("Skipping invalid worklist entry reason=%s", error)
        return False

    if not entry.get("Active", True):
        LOGGER.info("Skipping inactive worklist entry index=%s", index)
        return False

    expires_at = _text(entry.get("ExpiresAt"))
    if expires_at and _is_expired(_parse_expires_at(expires_at), now):
        LOGGER.info(
            "Skipping expired worklist entry index=%s expires_at=%s",
            index,
            expires_at,
        )
        return False

    return True


def read_worklist_payload(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        payload = json.load(source)

    if isinstance(payload, list):
        return {"entries": payload}
    if isinstance(payload, dict):
        return payload
    raise ValueError("worklist config must be an object with entries")


def write_worklist_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".worklist.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temp_path = Path(target.name)
            json.dump(payload, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def init_audit_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mwl_audit (
                accession_number TEXT PRIMARY KEY,
                chart_no TEXT,
                study_type TEXT,
                modality TEXT,
                station_aet TEXT,
                scheduled_at TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                cancelled_at TEXT,
                cancel_reason TEXT
            )
            """
        )


def _audit_values(entry: dict[str, Any], now: str) -> dict[str, str]:
    status = _entry_status(entry)
    return {
        "accession_number": _text(entry.get("AccessionNumber")),
        "chart_no": _text(entry.get("PatientID")),
        "study_type": (
            _text(entry.get("RequestedProcedureDescription"))
            or _text(entry.get("StudyDescription"))
            or _text(entry.get("ScheduledProcedureStepDescription"))
        ),
        "modality": _text(entry.get("Modality")),
        "station_aet": _text(entry.get("ScheduledStationAETitle")),
        "scheduled_at": _scheduled_at(entry),
        "status": status,
        "updated_at": now,
        "completed_at": _text(entry.get("CompletedAt")),
        "cancelled_at": _text(entry.get("CancelledAt")),
        "cancel_reason": _text(entry.get("CancelReason")),
    }


def upsert_audit_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    init_audit_db(path)
    now = _iso_now()
    with sqlite3.connect(path) as connection:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            values = _audit_values(entry, now)
            accession_number = values["accession_number"]
            if not accession_number:
                continue
            LOGGER.info("Upserting MWL audit %s status=%s", _entry_summary(entry), values["status"])
            connection.execute(
                """
                INSERT INTO mwl_audit (
                    accession_number,
                    chart_no,
                    study_type,
                    modality,
                    station_aet,
                    scheduled_at,
                    status,
                    created_at,
                    updated_at,
                    completed_at,
                    cancelled_at,
                    cancel_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(accession_number) DO UPDATE SET
                    chart_no=excluded.chart_no,
                    study_type=excluded.study_type,
                    modality=excluded.modality,
                    station_aet=excluded.station_aet,
                    scheduled_at=excluded.scheduled_at,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    completed_at=excluded.completed_at,
                    cancelled_at=excluded.cancelled_at,
                    cancel_reason=excluded.cancel_reason
                """,
                (
                    values["accession_number"],
                    values["chart_no"],
                    values["study_type"],
                    values["modality"],
                    values["station_aet"],
                    values["scheduled_at"],
                    values["status"],
                    now,
                    values["updated_at"],
                    values["completed_at"],
                    values["cancelled_at"],
                    values["cancel_reason"],
                ),
            )


def _load_worklist(path: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    payload = read_worklist_payload(path)

    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("worklist config must be an object with entries")

    effective_now = now or datetime.now().astimezone()
    normalized = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            LOGGER.warning("Skipping non-object worklist entry index=%s", index)
            continue
        if not _is_returnable_entry(entry, index, effective_now):
            continue
        normalized.append(entry)
    return normalized


def _entry_to_dataset(entry: dict[str, Any]) -> Dataset:
    dataset = Dataset()
    dataset.SpecificCharacterSet = _text(entry.get("SpecificCharacterSet")) or "ISO_IR 192"
    dataset.PatientName = _text(entry.get("PatientName"))
    dataset.PatientID = _text(entry.get("PatientID"))
    dataset.PatientBirthDate = _text(entry.get("PatientBirthDate"))
    dataset.PatientSex = _text(entry.get("PatientSex"))
    dataset.AccessionNumber = _text(entry.get("AccessionNumber"))
    dataset.StudyDescription = _text(entry.get("StudyDescription"))
    dataset.RequestedProcedureID = _text(entry.get("RequestedProcedureID")) or dataset.AccessionNumber
    dataset.RequestedProcedureDescription = _text(entry.get("RequestedProcedureDescription"))
    dataset.RequestedProcedurePriority = _text(entry.get("RequestedProcedurePriority")) or "ROUTINE"

    step = Dataset()
    step.Modality = _text(entry.get("Modality"))
    step.ScheduledStationAETitle = _text(entry.get("ScheduledStationAETitle"))
    step.ScheduledProcedureStepStartDate = _text(entry.get("ScheduledProcedureStepStartDate"))
    step.ScheduledProcedureStepStartTime = _text(entry.get("ScheduledProcedureStepStartTime"))
    step.ScheduledProcedureStepDescription = _text(
        entry.get("ScheduledProcedureStepDescription")
    )
    step.ScheduledProcedureStepID = (
        _text(entry.get("ScheduledProcedureStepID")) or dataset.AccessionNumber
    )

    dataset.ScheduledProcedureStepSequence = Sequence([step])
    return dataset


def load_worklist_datasets(
    path: Path = DEFAULT_WORKLIST_PATH,
    now: datetime | None = None,
) -> list[Dataset]:
    return [_entry_to_dataset(entry) for entry in _load_worklist(path, now=now)]


def _dataset_value(dataset: Dataset, keyword: str) -> str:
    return _text(getattr(dataset, keyword, ""))


def _query_step(identifier: Dataset) -> Dataset:
    sequence = getattr(identifier, "ScheduledProcedureStepSequence", None)
    if sequence:
        return sequence[0]
    return Dataset()


def matches_query(identifier: Dataset, item: Dataset) -> bool:
    requested_patient_id = _dataset_value(identifier, "PatientID")
    if requested_patient_id and requested_patient_id != _dataset_value(item, "PatientID"):
        return False

    requested_accession = _dataset_value(identifier, "AccessionNumber")
    if requested_accession and requested_accession != _dataset_value(item, "AccessionNumber"):
        return False

    requested_step = _query_step(identifier)
    item_step = item.ScheduledProcedureStepSequence[0]

    requested_modality = _dataset_value(requested_step, "Modality")
    if requested_modality and requested_modality != _dataset_value(item_step, "Modality"):
        return False

    requested_station = _dataset_value(requested_step, "ScheduledStationAETitle")
    if requested_station and requested_station != _dataset_value(item_step, "ScheduledStationAETitle"):
        return False

    return True


def _assoc_context(event: evt.Event) -> tuple[str, str, str]:
    assoc = event.assoc
    remote_ip = _text(getattr(assoc.requestor, "address", ""))
    calling_ae = _text(getattr(assoc.requestor, "ae_title", ""))
    called_ae = _text(getattr(assoc.acceptor, "ae_title", DEFAULT_AE_TITLE))
    return remote_ip, calling_ae, called_ae


def make_handle_find(worklist_path: Path):
    def handle_find(event: evt.Event):
        identifier = event.identifier
        remote_ip, calling_ae, called_ae = _assoc_context(event)
        query_step = _query_step(identifier)
        requested_patient_id = _dataset_value(identifier, "PatientID")
        requested_accession = _dataset_value(identifier, "AccessionNumber")
        requested_modality = _dataset_value(query_step, "Modality")
        requested_station = _dataset_value(query_step, "ScheduledStationAETitle")

        try:
            datasets = load_worklist_datasets(worklist_path)
        except Exception:
            LOGGER.exception("Failed to load worklist path=%s", worklist_path)
            yield 0xA700, None
            return

        matches = [dataset for dataset in datasets if matches_query(identifier, dataset)]
        LOGGER.info(
            "C-FIND query remote_ip=%s calling_ae=%s called_ae=%s patient_id=%r accession=%r modality=%r station_aet=%r loaded=%s matches=%s",
            remote_ip,
            calling_ae,
            called_ae,
            requested_patient_id,
            requested_accession,
            requested_modality,
            requested_station,
            len(datasets),
            len(matches),
        )
        for dataset in matches:
            LOGGER.info("C-FIND match %s", _item_summary(dataset))
            yield 0xFF00, dataset

        LOGGER.info(
            "C-FIND complete remote_ip=%s calling_ae=%s called_ae=%s matches=%s status=0x0000",
            remote_ip,
            calling_ae,
            called_ae,
            len(matches),
        )
        yield 0x0000, None

    return handle_find


def handle_echo(event: evt.Event) -> int:
    remote_ip, calling_ae, called_ae = _assoc_context(event)
    LOGGER.info(
        "C-ECHO remote_ip=%s calling_ae=%s called_ae=%s status=0x0000",
        remote_ip,
        calling_ae,
        called_ae,
    )
    return 0x0000


def _find_entries_by_accession(payload: dict[str, Any], accession_number: str) -> list[dict[str, Any]]:
    entries = payload.get("entries", [])
    return [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and _text(entry.get("AccessionNumber")) == accession_number
    ]


def apply_worklist_state(
    path: Path,
    accession_number: str,
    state: str,
    cancel_reason: str = "",
) -> tuple[dict[str, Any], int]:
    payload = read_worklist_payload(path)
    errors = validate_worklist_payload(payload)
    if errors:
        raise ValueError("; ".join(errors))

    matches = _find_entries_by_accession(payload, accession_number)
    if not matches:
        return payload, 0

    timestamp = _iso_now()
    for entry in matches:
        entry["Active"] = False
        if state == "complete":
            entry["CompletedAt"] = timestamp
        elif state == "cancel":
            entry["CancelledAt"] = timestamp
            if cancel_reason:
                entry["CancelReason"] = cancel_reason
        else:
            raise ValueError(f"unsupported state {state!r}")

    write_worklist_payload(path, payload)
    return payload, len(matches)


def _json_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_request_json(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        raw = b"{}"
    return json.loads(raw.decode("utf-8"))


def make_api_handler(worklist_path: Path, audit_db_path: Path):
    class WorklistApiHandler(BaseHTTPRequestHandler):
        server_version = "KaosPACSMWL/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("MWL API %s", format % args)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                _json_response(self, HTTPStatus.OK, {"status": "ok"})
                return
            if path == "/worklist":
                try:
                    payload = read_worklist_payload(worklist_path)
                except Exception:
                    LOGGER.exception("MWL API failed to read worklist path=%s", worklist_path)
                    _json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "failed to read worklist"},
                    )
                    return
                _json_response(self, HTTPStatus.OK, payload)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if path != "/worklist":
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            try:
                payload = _read_request_json(self)
            except json.JSONDecodeError as error:
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid JSON", "details": [str(error)]},
                )
                return

            errors = validate_worklist_payload(payload)
            if errors:
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid worklist", "details": errors},
                )
                return

            try:
                write_worklist_payload(worklist_path, payload)
                upsert_audit_entries(audit_db_path, payload.get("entries", []))
            except Exception:
                LOGGER.exception(
                    "MWL API failed to write worklist or audit path=%s audit_db=%s",
                    worklist_path,
                    audit_db_path,
                )
                _json_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "failed to write worklist"},
                )
                return

            LOGGER.info(
                "MWL API updated worklist path=%s entries=%s",
                worklist_path,
                len(payload.get("entries", [])),
            )
            _json_response(self, HTTPStatus.OK, payload)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in {"/worklist/complete", "/worklist/cancel"}:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            try:
                request = _read_request_json(self)
            except json.JSONDecodeError as error:
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid JSON", "details": [str(error)]},
                )
                return
            if not isinstance(request, dict):
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid request", "details": ["body must be an object"]},
                )
                return

            accession_number = _text(request.get("AccessionNumber"))
            if not accession_number:
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "invalid request",
                        "details": ["AccessionNumber is required"],
                    },
                )
                return

            state = "complete" if path == "/worklist/complete" else "cancel"
            try:
                payload, updated = apply_worklist_state(
                    worklist_path,
                    accession_number=accession_number,
                    state=state,
                    cancel_reason=_text(request.get("CancelReason")),
                )
                if updated:
                    upsert_audit_entries(
                        audit_db_path,
                        _find_entries_by_accession(payload, accession_number),
                    )
            except ValueError as error:
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid worklist", "details": [str(error)]},
                )
                return
            except Exception:
                LOGGER.exception("MWL API failed to update worklist state path=%s", worklist_path)
                _json_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "failed to update worklist"},
                )
                return

            if not updated:
                _json_response(
                    self,
                    HTTPStatus.NOT_FOUND,
                    {"error": "not found", "details": ["AccessionNumber not found"]},
                )
                return

            LOGGER.info(
                "MWL API state update state=%s accession=%s updated=%s",
                state,
                accession_number,
                updated,
            )
            _json_response(
                self,
                HTTPStatus.OK,
                {"updated": updated, "worklist": payload},
            )

    return WorklistApiHandler


def start_api_server(
    host: str,
    port: int,
    worklist_path: Path,
    audit_db_path: Path,
) -> ThreadingHTTPServer:
    init_audit_db(audit_db_path)
    server = ThreadingHTTPServer((host, port), make_api_handler(worklist_path, audit_db_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    LOGGER.info(
        "Started KaosPACS MWL API host=%s port=%s worklist=%s audit_db=%s",
        host,
        port,
        worklist_path,
        audit_db_path,
    )
    return server


def start_server(
    ae_title: str,
    port: int,
    worklist_path: Path,
    block: bool = True,
):
    ae = AE(ae_title=ae_title)
    ae.add_supported_context(ModalityWorklistInformationFind)
    ae.add_supported_context(Verification)

    LOGGER.info(
        "Starting KaosPACS MWL SCP ae_title=%s port=%s worklist=%s",
        ae_title,
        port,
        worklist_path,
    )
    return ae.start_server(
        ("", port),
        block=block,
        evt_handlers=[
            (evt.EVT_C_FIND, make_handle_find(worklist_path)),
            (evt.EVT_C_ECHO, handle_echo),
        ],
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ae_title = os.getenv("MWL_AET", DEFAULT_AE_TITLE)
    port = int(os.getenv("MWL_PORT", str(DEFAULT_PORT)))
    api_host = os.getenv("MWL_API_HOST", DEFAULT_API_HOST)
    api_port = int(os.getenv("MWL_API_PORT", str(DEFAULT_API_PORT)))
    worklist_path = Path(os.getenv("WORKLIST_PATH", str(DEFAULT_WORKLIST_PATH)))
    audit_db_path = Path(os.getenv("MWL_AUDIT_DB", str(DEFAULT_AUDIT_DB_PATH)))
    LOGGER.info("Runtime timestamp=%s", datetime.now().isoformat(timespec="seconds"))
    start_api_server(
        host=api_host,
        port=api_port,
        worklist_path=worklist_path,
        audit_db_path=audit_db_path,
    )
    start_server(ae_title=ae_title, port=port, worklist_path=worklist_path)


if __name__ == "__main__":
    main()
