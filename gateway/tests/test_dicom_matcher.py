from pydicom.dataset import Dataset

from app.dicom.matcher import match_dataset_to_worklist


def dataset_with(**values):
    dataset = Dataset()
    for key, value in values.items():
        setattr(dataset, key, value)
    return dataset


def worklist_with(*entries):
    return {"entries": list(entries)}


def active_entry(**values):
    entry = {"Active": True}
    entry.update(values)
    return entry


def inactive_entry(**values):
    entry = {"Active": False}
    entry.update(values)
    return entry


def test_exact_accession_number_match() -> None:
    result = match_dataset_to_worklist(
        dataset_with(AccessionNumber="A1"),
        worklist_with(active_entry(AccessionNumber="A1")),
    )

    assert result.matched is True
    assert result.matched_by == "AccessionNumber"
    assert result.accession_number == "A1"


def test_requested_procedure_id_match() -> None:
    result = match_dataset_to_worklist(
        dataset_with(RequestedProcedureID="RP1"),
        worklist_with(active_entry(AccessionNumber="A1", RequestedProcedureID="RP1")),
    )

    assert result.matched is True
    assert result.matched_by == "RequestedProcedureID"
    assert result.accession_number == "A1"


def test_scheduled_procedure_step_id_match() -> None:
    result = match_dataset_to_worklist(
        dataset_with(ScheduledProcedureStepID="SPS1"),
        worklist_with(active_entry(AccessionNumber="A1", ScheduledProcedureStepID="SPS1")),
    )

    assert result.matched is True
    assert result.matched_by == "ScheduledProcedureStepID"
    assert result.accession_number == "A1"


def test_inactive_entry_ignored() -> None:
    result = match_dataset_to_worklist(
        dataset_with(AccessionNumber="A1"),
        worklist_with(inactive_entry(AccessionNumber="A1")),
    )

    assert result.matched is False
    assert result.reason == "no_active_match"


def test_no_match_returns_false() -> None:
    result = match_dataset_to_worklist(
        dataset_with(AccessionNumber="A1"),
        worklist_with(active_entry(AccessionNumber="A2")),
    )

    assert result.matched is False
    assert result.matched_by is None
    assert result.accession_number == "A1"


def test_duplicate_accessions_first_active_entry_wins() -> None:
    first = active_entry(AccessionNumber="A1", RequestedProcedureID="FIRST")
    second = active_entry(AccessionNumber="A1", RequestedProcedureID="SECOND")

    result = match_dataset_to_worklist(
        dataset_with(AccessionNumber="A1"),
        worklist_with(first, second),
    )

    assert result.matched is True
    assert result.worklist_entry is first


def test_patient_name_is_never_used_for_matching() -> None:
    result = match_dataset_to_worklist(
        dataset_with(PatientName="SAME^NAME"),
        worklist_with(active_entry(AccessionNumber="A1", PatientName="SAME^NAME")),
    )

    assert result.matched is False


def test_patient_id_is_never_used_for_matching() -> None:
    result = match_dataset_to_worklist(
        dataset_with(PatientID="P1"),
        worklist_with(active_entry(AccessionNumber="A1", PatientID="P1")),
    )

    assert result.matched is False
