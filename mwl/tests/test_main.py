import sys
from pathlib import Path

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import load_worklist_datasets, matches_query


def test_load_worklist_maps_hardcoded_bmd_item():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"

    datasets = load_worklist_datasets(path)

    assert len(datasets) == 2
    dataset = datasets[0]
    assert dataset.PatientName == "TEST^BMD"
    assert dataset.PatientID == "KAOSMWL001"
    assert dataset.AccessionNumber == "KAOSMWL001"
    assert dataset.ScheduledProcedureStepSequence[0].Modality == "BMD"
    assert dataset.ScheduledProcedureStepSequence[0].ScheduledStationAETitle == "BMD"


def test_matches_query_by_patient_and_station():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    item = load_worklist_datasets(path)[0]

    query = Dataset()
    query.PatientID = "KAOSMWL001"
    step = Dataset()
    step.ScheduledStationAETitle = "BMD"
    query.ScheduledProcedureStepSequence = Sequence([step])

    assert matches_query(query, item)


def test_filters_multiple_entries_by_modality_and_station():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    items = load_worklist_datasets(path)

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
    items = load_worklist_datasets(path)

    query = Dataset()
    query.AccessionNumber = "KAOSMWL002"

    matches = [item for item in items if matches_query(query, item)]

    assert len(matches) == 1
    assert matches[0].PatientName == "TEST^XRAY"


def test_non_matching_accession_is_rejected():
    path = Path(__file__).resolve().parents[1] / "config" / "worklist.json"
    item = load_worklist_datasets(path)[0]

    query = Dataset()
    query.AccessionNumber = "OTHER"

    assert not matches_query(query, item)
