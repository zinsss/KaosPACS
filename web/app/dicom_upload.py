from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime

from PIL import Image
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    EncapsulatedPDFStorage,
    ExplicitVRLittleEndian,
    SecondaryCaptureImageStorage,
    generate_uid,
)


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png"}
SUPPORTED_PDF_TYPES = {"application/pdf"}


@dataclass(frozen=True)
class UploadDicomResult:
    dicom_bytes: bytes
    modality: str
    study_description: str
    accession_number: str


def create_upload_dicom(
    *,
    patient_id: str,
    patient_name: str,
    filename: str,
    content_type: str,
    content: bytes,
    patient_birth_date: str = "",
    patient_sex: str = "",
    now: datetime | None = None,
) -> UploadDicomResult:
    normalized_type = _content_type(content_type, filename)
    now = now or datetime.now()
    accession_number = f"UP{now.strftime('%y%m%d%H%M%S')}"

    if normalized_type in SUPPORTED_IMAGE_TYPES:
        dataset = _image_dataset(
            patient_id=patient_id,
            patient_name=patient_name,
            patient_birth_date=patient_birth_date,
            patient_sex=patient_sex,
            filename=filename,
            content=content,
            now=now,
            accession_number=accession_number,
        )
        description = "Uploaded image"
    elif normalized_type in SUPPORTED_PDF_TYPES:
        dataset = _pdf_dataset(
            patient_id=patient_id,
            patient_name=patient_name,
            patient_birth_date=patient_birth_date,
            patient_sex=patient_sex,
            filename=filename,
            content=content,
            now=now,
            accession_number=accession_number,
        )
        description = "Uploaded PDF"
    else:
        raise ValueError("unsupported_upload_type")

    return UploadDicomResult(
        dicom_bytes=_write_dataset(dataset),
        modality="DOC",
        study_description=description,
        accession_number=accession_number,
    )


def _base_dataset(
    *,
    patient_id: str,
    patient_name: str,
    patient_birth_date: str,
    patient_sex: str,
    filename: str,
    now: datetime,
    accession_number: str,
    sop_class_uid: str,
    study_description: str,
) -> Dataset:
    sop_instance_uid = generate_uid()
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.FileMetaInformationVersion = b"\x00\x01"
    dataset.file_meta.MediaStorageSOPClassUID = sop_class_uid
    dataset.file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset.file_meta.ImplementationClassUID = generate_uid()
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.SOPClassUID = sop_class_uid
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.StudyDate = now.strftime("%Y%m%d")
    dataset.StudyTime = now.strftime("%H%M%S")
    dataset.ContentDate = dataset.StudyDate
    dataset.ContentTime = dataset.StudyTime
    dataset.AccessionNumber = accession_number
    dataset.Modality = "DOC"
    dataset.PatientID = patient_id
    if patient_name:
        dataset.PatientName = patient_name
    normalized_birth_date = _dicom_birth_date(patient_birth_date)
    if normalized_birth_date:
        dataset.PatientBirthDate = normalized_birth_date
    normalized_sex = _dicom_patient_sex(patient_sex)
    if normalized_sex:
        dataset.PatientSex = normalized_sex
    dataset.StudyDescription = study_description
    dataset.SeriesDescription = "KaosPACS manual upload"
    dataset.Manufacturer = "KaosPACS"
    dataset.ConversionType = "WSD"
    dataset.ImageComments = f"Uploaded through KaosPACS Web: {filename[:180]}"
    return dataset


def _image_dataset(
    *,
    patient_id: str,
    patient_name: str,
    patient_birth_date: str,
    patient_sex: str,
    filename: str,
    content: bytes,
    now: datetime,
    accession_number: str,
) -> Dataset:
    dataset = _base_dataset(
        patient_id=patient_id,
        patient_name=patient_name,
        patient_birth_date=patient_birth_date,
        patient_sex=patient_sex,
        filename=filename,
        now=now,
        accession_number=accession_number,
        sop_class_uid=SecondaryCaptureImageStorage,
        study_description="Uploaded image",
    )
    image = Image.open(io.BytesIO(content))
    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    dataset.Rows = image.height
    dataset.Columns = image.width
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    if image.mode == "L":
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
    else:
        dataset.SamplesPerPixel = 3
        dataset.PhotometricInterpretation = "RGB"
        dataset.PlanarConfiguration = 0
    dataset.PixelData = image.tobytes()
    return dataset


def _pdf_dataset(
    *,
    patient_id: str,
    patient_name: str,
    patient_birth_date: str,
    patient_sex: str,
    filename: str,
    content: bytes,
    now: datetime,
    accession_number: str,
) -> Dataset:
    if not content.startswith(b"%PDF"):
        raise ValueError("invalid_pdf")
    dataset = _base_dataset(
        patient_id=patient_id,
        patient_name=patient_name,
        patient_birth_date=patient_birth_date,
        patient_sex=patient_sex,
        filename=filename,
        now=now,
        accession_number=accession_number,
        sop_class_uid=EncapsulatedPDFStorage,
        study_description="Uploaded PDF",
    )
    dataset.MIMETypeOfEncapsulatedDocument = "application/pdf"
    dataset.EncapsulatedDocument = content
    dataset.BurnedInAnnotation = "NO"
    return dataset


def _write_dataset(dataset: Dataset) -> bytes:
    buffer = io.BytesIO()
    dataset.save_as(buffer, enforce_file_format=True)
    return buffer.getvalue()


def _content_type(content_type: str, filename: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if content_type:
        return content_type
    if suffix in {"jpg", "jpeg"}:
        return "image/jpeg"
    if suffix == "png":
        return "image/png"
    if suffix == "pdf":
        return "application/pdf"
    return "application/octet-stream"


def _dicom_birth_date(value: str) -> str:
    digits = "".join(char for char in value if char.isdigit())
    return digits if len(digits) == 8 else ""


def _dicom_patient_sex(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"M", "MALE"} or value.strip() == "남":
        return "M"
    if normalized in {"F", "FEMALE"} or value.strip() == "여":
        return "F"
    if normalized in {"O", "OTHER"}:
        return "O"
    return ""
