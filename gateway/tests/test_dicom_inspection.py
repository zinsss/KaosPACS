import json

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from app.dicom.inspection import append_inspection_report, inspect_dataset


def _dataset() -> Dataset:
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = generate_uid()
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.AccessionNumber = "ACC-INSPECT"
    dataset.Modality = "OT"
    dataset.PatientName = "SECRET^NAME"
    dataset.PatientID = "SECRETID"
    dataset.StudyDescription = "SECRET STUDY"
    dataset.SeriesDescription = "SECRET SERIES"
    scheduled_step = Dataset()
    scheduled_step.ScheduledProcedureStepDescription = "SECRET STEP"
    dataset.ScheduledProcedureStepSequence = Sequence([scheduled_step])
    return dataset


def test_iso_ir_149_marks_charset_review() -> None:
    dataset = _dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"

    report = inspect_dataset(dataset)

    assert report.needs_charset_review is True
    assert report.review_reasons == [
        "specific_character_set_iso_ir_149",
        "korean_text_possible",
    ]
    assert report.specific_character_set == ["ISO_IR 149"]


def test_iso_ir_192_normal_case_does_not_mark_review() -> None:
    dataset = _dataset()
    dataset.SpecificCharacterSet = "ISO_IR 192"

    report = inspect_dataset(dataset)

    assert report.needs_charset_review is False
    assert report.review_reasons == []


def test_missing_charset_with_text_tags_marks_review() -> None:
    dataset = _dataset()

    report = inspect_dataset(dataset)

    assert report.needs_charset_review is True
    assert report.review_reasons == [
        "missing_specific_character_set_with_text_tags",
        "korean_text_possible",
    ]


def test_unknown_charset_marks_review() -> None:
    dataset = _dataset()
    dataset.SpecificCharacterSet = "X_UNKNOWN"

    report = inspect_dataset(dataset)

    assert report.needs_charset_review is True
    assert report.review_reasons == ["unsupported_specific_character_set"]


def test_inspection_report_contains_presence_only_without_phi_values(tmp_path) -> None:
    dataset = _dataset()
    dataset.SpecificCharacterSet = "ISO_IR 149"
    report_path = tmp_path / "dicom_inspection.jsonl"

    report = inspect_dataset(dataset)
    append_inspection_report(report_path, report)
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["accession_number"] == "ACC-INSPECT"
    assert payload["text_tag_presence"]["PatientName"] is True
    assert payload["text_tag_presence"]["PatientID"] is True
    assert payload["text_tag_presence"]["StudyDescription"] is True
    assert payload["text_tag_presence"]["ScheduledProcedureStepDescription"] is True
    assert payload["text_vr_counts"]["PN"] >= 1
    assert payload["text_vr_counts"]["LO"] >= 1
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "SECRET^NAME" not in serialized
    assert "SECRETID" not in serialized
    assert "SECRET STUDY" not in serialized
    assert "SECRET SERIES" not in serialized
    assert "SECRET STEP" not in serialized
