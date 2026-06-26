from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pynetdicom import AE, evt
from pynetdicom.sop_class import ModalityWorklistInformationFind, Verification


DEFAULT_AE_TITLE = "VIEWREX_WL"
DEFAULT_PORT = 105
DEFAULT_WORKLIST_PATH = Path("/app/config/worklist.json")

LOGGER = logging.getLogger("kaospacs.mwl")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _load_worklist(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        payload = json.load(source)

    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("worklist config must be a list or an object with entries")

    normalized = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            LOGGER.warning("Skipping non-object worklist entry index=%s", index)
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


def load_worklist_datasets(path: Path = DEFAULT_WORKLIST_PATH) -> list[Dataset]:
    return [_entry_to_dataset(entry) for entry in _load_worklist(path)]


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
        for dataset in matches:
            yield 0xFF00, dataset

        LOGGER.info(
            "C-FIND remote_ip=%s calling_ae=%s called_ae=%s patient_id=%s accession=%s modality=%s station_aet=%s matches=%s",
            remote_ip,
            calling_ae,
            called_ae,
            requested_patient_id,
            requested_accession,
            requested_modality,
            requested_station,
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
    worklist_path = Path(os.getenv("WORKLIST_PATH", str(DEFAULT_WORKLIST_PATH)))
    LOGGER.info("Runtime timestamp=%s", datetime.now().isoformat(timespec="seconds"))
    start_server(ae_title=ae_title, port=port, worklist_path=worklist_path)


if __name__ == "__main__":
    main()
