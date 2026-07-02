import json

from pydicom import dcmread
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.multival import MultiValue
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from pydicom.valuerep import PersonName

from app.dicom.charset_fix import (
    UTF8_CHARSET,
    append_charset_fix_report,
    log_charset_fix_result,
    maybe_fix_charset,
)


def _dataset() -> Dataset:
    sop_instance_uid = generate_uid()
    study_instance_uid = generate_uid()
    series_instance_uid = generate_uid()
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    dataset.file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.StudyInstanceUID = study_instance_uid
    dataset.SeriesInstanceUID = series_instance_uid
    dataset.AccessionNumber = "ACC-FIX"
    dataset.Modality = "OT"
    dataset.SpecificCharacterSet = "ISO_IR 149"
    dataset.PatientName = "테스트^환자"
    dataset.PatientID = "PID-STABLE"
    dataset.StudyDescription = "검사 설명"
    dataset.SeriesDescription = "시리즈 설명"
    dataset.RequestedProcedureDescription = "요청 검사"
    dataset.InstitutionName = "기관명"
    dataset.ReferringPhysicianName = "의사^참조"
    dataset.PerformingPhysicianName = "의사^시행"
    scheduled_step = Dataset()
    scheduled_step.ScheduledProcedureStepDescription = "예약 검사"
    dataset.ScheduledProcedureStepSequence = Sequence([scheduled_step])
    dataset.add_new((0x0011, 0x0010), "LO", "PRIVATE_CREATOR")
    dataset.add_new((0x0011, 0x1001), "LO", "PRIVATE VALUE")
    dataset.Rows = 1
    dataset.Columns = 1
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = b"\x7f"
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False
    return dataset


def test_charset_fix_disabled_does_not_modify_dataset(tmp_path) -> None:
    dataset = _dataset()
    before = json.loads(dataset.to_json())

    result = maybe_fix_charset(dataset, enabled=False, mode="iso_ir_149_to_utf8")
    append_charset_fix_report(tmp_path / "fix.jsonl", result)

    assert result.fix_applied is False
    assert result.reason == "disabled"
    assert result.error_code == "skipped_disabled"
    assert result.dataset is dataset
    assert json.loads(dataset.to_json()) == before
    report = json.loads((tmp_path / "fix.jsonl").read_text(encoding="utf-8"))
    assert report["fix_applied"] is False
    assert report["reason"] == "disabled"
    assert "테스트" not in json.dumps(report, ensure_ascii=False)
    assert "PID-STABLE" not in json.dumps(report, ensure_ascii=False)


def test_charset_fix_mode_off_does_not_modify_dataset() -> None:
    dataset = _dataset()

    result = maybe_fix_charset(dataset, enabled=True, mode="off")

    assert result.fix_applied is False
    assert result.reason == "mode_off"
    assert result.error_code == "skipped_mode_off"
    assert dataset.SpecificCharacterSet == "ISO_IR 149"


def test_iso_ir_149_to_utf8_preserves_identity_and_pixel_data(tmp_path) -> None:
    dataset = _dataset()
    identity = {
        "SOPInstanceUID": str(dataset.SOPInstanceUID),
        "StudyInstanceUID": str(dataset.StudyInstanceUID),
        "SeriesInstanceUID": str(dataset.SeriesInstanceUID),
        "AccessionNumber": str(dataset.AccessionNumber),
        "Modality": str(dataset.Modality),
        "PatientID": str(dataset.PatientID),
        "PixelData": bytes(dataset.PixelData),
    }

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")

    assert result.fix_applied is True
    assert result.original_specific_character_set == ["ISO_IR 149"]
    assert result.new_specific_character_set == [UTF8_CHARSET]
    assert result.dataset.SpecificCharacterSet == UTF8_CHARSET
    assert str(result.dataset.SOPInstanceUID) == identity["SOPInstanceUID"]
    assert str(result.dataset.StudyInstanceUID) == identity["StudyInstanceUID"]
    assert str(result.dataset.SeriesInstanceUID) == identity["SeriesInstanceUID"]
    assert str(result.dataset.AccessionNumber) == identity["AccessionNumber"]
    assert str(result.dataset.Modality) == identity["Modality"]
    assert str(result.dataset.PatientID) == identity["PatientID"]
    assert bytes(result.dataset.PixelData) == identity["PixelData"]
    assert result.dataset.PatientName == "테스트^환자"
    assert result.dataset.StudyDescription == "검사 설명"
    assert (
        result.dataset.ScheduledProcedureStepSequence[0]
        .ScheduledProcedureStepDescription
        == "예약 검사"
    )
    assert "PatientID" in result.skipped_keywords
    assert "AccessionNumber" in result.skipped_keywords
    assert "PatientName" in result.fixed_keywords
    assert "ScheduledProcedureStepDescription" in result.fixed_keywords

    output_path = tmp_path / "fixed.dcm"
    result.dataset.save_as(output_path, write_like_original=False)
    reread = dcmread(output_path)
    assert reread.SpecificCharacterSet == UTF8_CHARSET
    assert str(reread.PatientName) == "테스트^환자"
    assert reread.PatientID == "PID-STABLE"


def test_person_name_component_groups_remain_equivalent_after_save_reread(
    tmp_path,
) -> None:
    dataset = _dataset()
    dataset.PatientName = PersonName("Hong^Gildong=홍^길동=Hong^GilDong")
    dataset.ReferringPhysicianName = PersonName("Refer^Doctor=참조^의사")
    dataset.PerformingPhysicianName = PersonName("Perform^Doctor=시행^의사")

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")

    assert result.fix_applied is True
    assert isinstance(result.dataset.PatientName, PersonName)
    assert str(result.dataset.PatientName) == "Hong^Gildong=홍^길동=Hong^GilDong"
    assert str(result.dataset.ReferringPhysicianName) == "Refer^Doctor=참조^의사"
    assert str(result.dataset.PerformingPhysicianName) == "Perform^Doctor=시행^의사"
    assert "PatientName" in result.fixed_keywords
    assert "ReferringPhysicianName" in result.fixed_keywords
    assert "PerformingPhysicianName" in result.fixed_keywords

    output_path = tmp_path / "fixed-pn.dcm"
    result.dataset.save_as(output_path, write_like_original=False)
    reread = dcmread(output_path)
    assert reread.SpecificCharacterSet == UTF8_CHARSET
    assert str(reread.PatientName) == "Hong^Gildong=홍^길동=Hong^GilDong"
    assert str(reread.ReferringPhysicianName) == "Refer^Doctor=참조^의사"
    assert str(reread.PerformingPhysicianName) == "Perform^Doctor=시행^의사"


def test_multivalue_lo_text_preserves_multiplicity_after_save_reread(
    tmp_path,
) -> None:
    dataset = _dataset()
    dataset.StudyDescription = MultiValue(str, ["검사1", "검사2"])
    dataset.SeriesDescription = MultiValue(str, ["시리즈1", "시리즈2"])

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")

    assert result.fix_applied is True
    assert isinstance(result.dataset.StudyDescription, MultiValue)
    assert list(result.dataset.StudyDescription) == ["검사1", "검사2"]
    assert isinstance(result.dataset.SeriesDescription, MultiValue)
    assert list(result.dataset.SeriesDescription) == ["시리즈1", "시리즈2"]

    output_path = tmp_path / "fixed-multivalue.dcm"
    result.dataset.save_as(output_path, write_like_original=False)
    reread = dcmread(output_path)
    assert reread.SpecificCharacterSet == UTF8_CHARSET
    assert list(reread.StudyDescription) == ["검사1", "검사2"]
    assert list(reread.SeriesDescription) == ["시리즈1", "시리즈2"]


def test_already_decoded_korean_str_remains_unchanged_before_reserialization() -> None:
    dataset = _dataset()
    original_study_description = dataset.StudyDescription
    original_series_description = dataset.SeriesDescription

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")

    assert result.fix_applied is True
    assert result.dataset.StudyDescription == original_study_description
    assert result.dataset.SeriesDescription == original_series_description


def test_missing_charset_with_euc_kr_mojibake_is_normalized_to_utf8(
    tmp_path,
) -> None:
    dataset = _dataset()
    del dataset.SpecificCharacterSet
    dataset.PatientName = "ÀÌÁø¼º"
    dataset.StudyDescription = "¼ö°üÀý2¸Å"
    dataset.SeriesDescription = "AP (B)"
    identity = {
        "SOPInstanceUID": str(dataset.SOPInstanceUID),
        "StudyInstanceUID": str(dataset.StudyInstanceUID),
        "SeriesInstanceUID": str(dataset.SeriesInstanceUID),
        "AccessionNumber": str(dataset.AccessionNumber),
        "PatientID": str(dataset.PatientID),
        "PixelData": bytes(dataset.PixelData),
    }

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")

    assert result.fix_applied is True
    assert result.reason == "missing_charset_euc_kr_to_utf8_applied"
    assert result.original_specific_character_set == []
    assert result.new_specific_character_set == [UTF8_CHARSET]
    assert result.dataset.SpecificCharacterSet == UTF8_CHARSET
    assert str(result.dataset.PatientName) == "이진성"
    assert result.dataset.StudyDescription == "수관절2매"
    assert result.dataset.SeriesDescription == "AP (B)"
    assert str(result.dataset.SOPInstanceUID) == identity["SOPInstanceUID"]
    assert str(result.dataset.StudyInstanceUID) == identity["StudyInstanceUID"]
    assert str(result.dataset.SeriesInstanceUID) == identity["SeriesInstanceUID"]
    assert str(result.dataset.AccessionNumber) == identity["AccessionNumber"]
    assert str(result.dataset.PatientID) == identity["PatientID"]
    assert bytes(result.dataset.PixelData) == identity["PixelData"]

    output_path = tmp_path / "missing-charset-fixed.dcm"
    result.dataset.save_as(output_path, write_like_original=False)
    reread = dcmread(output_path)
    assert reread.SpecificCharacterSet == UTF8_CHARSET
    assert str(reread.PatientName) == "이진성"
    assert reread.StudyDescription == "수관절2매"


def test_missing_charset_with_ascii_text_is_skipped_without_guessing() -> None:
    dataset = _dataset()
    del dataset.SpecificCharacterSet
    dataset.PatientName = "ASCII^ONLY"
    dataset.StudyDescription = "ASCII STUDY"

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")

    assert result.fix_applied is False
    assert result.reason == "not_target_charset"
    assert result.error_code == "skipped_not_target_charset"
    assert not hasattr(result.dataset, "SpecificCharacterSet")


def test_non_target_charsets_are_skipped_without_guessing() -> None:
    utf8_dataset = _dataset()
    utf8_dataset.SpecificCharacterSet = "ISO_IR 192"
    unknown_dataset = _dataset()
    unknown_dataset.SpecificCharacterSet = "X_UNKNOWN"

    for dataset in (utf8_dataset, unknown_dataset):
        result = maybe_fix_charset(
            dataset,
            enabled=True,
            mode="iso_ir_149_to_utf8",
        )

        assert result.fix_applied is False
        assert result.reason == "not_target_charset"
        assert result.error_code == "skipped_not_target_charset"


def test_private_text_tags_are_not_rewritten() -> None:
    dataset = _dataset()

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")

    assert result.fix_applied is True
    assert result.dataset[(0x0011, 0x1001)].value == "PRIVATE VALUE"
    assert "PRIVATE VALUE" not in json.dumps(
        result.to_report_dict(),
        ensure_ascii=False,
    )


def test_charset_fix_report_contains_keywords_only_without_values(tmp_path) -> None:
    dataset = _dataset()
    report_path = tmp_path / "fix.jsonl"

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")
    append_charset_fix_report(report_path, result)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["fix_applied"] is True
    assert "PatientName" in report["fixed_keywords"]
    assert "PatientID" in report["skipped_keywords"]
    assert "테스트" not in serialized
    assert "환자" not in serialized
    assert "PID-STABLE" not in serialized
    assert "PRIVATE VALUE" not in serialized


def test_charset_fix_logs_contain_no_phi_values(caplog) -> None:
    dataset = _dataset()
    caplog.set_level("INFO")

    result = maybe_fix_charset(dataset, enabled=True, mode="iso_ir_149_to_utf8")
    log_charset_fix_result(result)

    assert "테스트" not in caplog.text
    assert "환자" not in caplog.text
    assert "PID-STABLE" not in caplog.text
