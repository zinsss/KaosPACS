import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import load_worklist_datasets, matches_query


NOW = datetime(2026, 6, 27, 9, 0, 0, tzinfo=timezone.utc)


def write_worklist(tmp_path, entries):
    path = tmp_path / "worklist.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def valid_entry(**overrides):
    entry = {
        "Active": True,
        "ExpiresAt": "2099-12-31T23:59:59+09:00",
        "SpecificCharacterSet": "ISO_IR 192",
        "PatientName": "TEST^VALID",
        "PatientID": "VALID001",
        "PatientBirthDate": "19700101",
        "PatientSex": "O",
        "AccessionNumber": "VALID001",
        "Modality": "BMD",
        "ScheduledStationAETitle": "BMD",
        "ScheduledProcedureStepDescription": "BMD TEST",
        "StudyDescription": "BMD TEST",
        "RequestedProcedureDescription": "BMD TEST",
        "RequestedProcedureID": "VALID001",
        "ScheduledProcedureStepID": "VALID001",
        "ScheduledProcedureStepStartDate": "20260627",
        "ScheduledProcedureStepStartTime": "090000",
    }
    entry.update(overrides)
    return entry


def test_load_worklist_maps_hardcoded_bmd_item():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 2
    dataset = datasets[0]
    assert dataset.PatientName == "TEST^BMD"
    assert dataset.PatientID == "KAOSMWL001"
    assert dataset.AccessionNumber == "KAOSMWL001"
    assert dataset.ScheduledProcedureStepSequence[0].Modality == "BMD"
    assert dataset.ScheduledProcedureStepSequence[0].ScheduledStationAETitle == "BMD"


def test_matches_query_by_patient_and_station():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    item = load_worklist_datasets(path, now=NOW)[0]

    query = Dataset()
    query.PatientID = "KAOSMWL001"
    step = Dataset()
    step.ScheduledStationAETitle = "BMD"
    query.ScheduledProcedureStepSequence = Sequence([step])

    assert matches_query(query, item)


def test_filters_multiple_entries_by_modality_and_station():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    items = load_worklist_datasets(path, now=NOW)

    query = Dataset()
    step = Dataset()
    step.Modality = "CR"
    step.ScheduledStationAETitle = "INNOVISION"
    query.ScheduledProcedureStepSequence = Sequence([step])

    matches = [item for item in items if matches_query(query, item)]

    assert len(matches) == 1
    assert matches[0].PatientID == "KAOSMWL002"


def test_filters_by_accession_number():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    items = load_worklist_datasets(path, now=NOW)

    query = Dataset()
    query.AccessionNumber = "KAOSMWL002"

    matches = [item for item in items if matches_query(query, item)]

    assert len(matches) == 1
    assert matches[0].PatientName == "TEST^XRAY"


def test_non_matching_accession_is_rejected():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    item = load_worklist_datasets(path, now=NOW)[0]

    query = Dataset()
    query.AccessionNumber = "OTHER"

    assert not matches_query(query, item)


def test_valid_entry_is_returned(tmp_path):
    path = write_worklist(tmp_path, [valid_entry()])

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 1
    assert datasets[0].PatientID == "VALID001"


def test_inactive_entry_is_not_returned(tmp_path):
    path = write_worklist(tmp_path, [valid_entry(Active=False)])

    datasets = load_worklist_datasets(path, now=NOW)

    assert datasets == []


def test_expired_entry_is_not_returned(tmp_path):
    path = write_worklist(tmp_path, [valid_entry(ExpiresAt="2000-01-01T00:00:00+00:00")])

    datasets = load_worklist_datasets(path, now=NOW)

    assert datasets == []


def test_invalid_entry_missing_required_field_is_skipped(tmp_path, caplog):
    path = write_worklist(
        tmp_path,
        [
            valid_entry(PatientID=""),
            valid_entry(PatientID="VALID002", AccessionNumber="VALID002"),
        ],
    )

    datasets = load_worklist_datasets(path, now=NOW)

    assert len(datasets) == 1
    assert datasets[0].PatientID == "VALID002"
    assert "missing required fields" in caplog.text
    assert "PatientID" in caplog.text


def test_invalid_expires_at_is_skipped(tmp_path, caplog):
    path = write_worklist(tmp_path, [valid_entry(ExpiresAt="not-a-date")])

    datasets = load_worklist_datasets(path, now=NOW)

    assert datasets == []
    assert "invalid ExpiresAt" in caplog.text
