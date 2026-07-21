from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime

from PIL import Image
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    SecondaryCaptureImageStorage,
    generate_uid,
)


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png"}
SUPPORTED_PDF_TYPES = {"application/pdf"}
PDF_RENDER_SCALE = 2.0
PDF_MAX_PAGES = 10


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
    upload_index: int = 1,
    upload_count: int = 1,
    now: datetime | None = None,
) -> UploadDicomResult:
    return create_upload_dicoms(
        patient_id=patient_id,
        patient_name=patient_name,
        filename=filename,
        content_type=content_type,
        content=content,
        patient_birth_date=patient_birth_date,
        patient_sex=patient_sex,
        upload_index=upload_index,
        upload_count=upload_count,
        now=now,
    )[0]


def create_upload_dicoms(
    *,
    patient_id: str,
    patient_name: str,
    filename: str,
    content_type: str,
    content: bytes,
    patient_birth_date: str = "",
    patient_sex: str = "",
    upload_index: int = 1,
    upload_count: int = 1,
    now: datetime | None = None,
) -> list[UploadDicomResult]:
    normalized_type = _content_type(content_type, filename)
    now = now or datetime.now()
    accession_number = _upload_accession_number(now, upload_index, upload_count)

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
            upload_index=upload_index,
            upload_count=upload_count,
        )
        return [
            UploadDicomResult(
                dicom_bytes=_write_dataset(dataset),
                modality="DOC",
                study_description="Uploaded image",
                accession_number=accession_number,
            )
        ]
    elif normalized_type in SUPPORTED_PDF_TYPES:
        return _pdf_datasets(
            patient_id=patient_id,
            patient_name=patient_name,
            patient_birth_date=patient_birth_date,
            patient_sex=patient_sex,
            filename=filename,
            content=content,
            now=now,
            accession_number=accession_number,
            upload_index=upload_index,
            upload_count=upload_count,
        )

    raise ValueError("unsupported_upload_type")


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
    upload_index: int,
    upload_count: int,
    study_instance_uid: str | None = None,
    series_instance_uid: str | None = None,
    instance_number: int | None = None,
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
    dataset.StudyInstanceUID = study_instance_uid or generate_uid()
    dataset.SeriesInstanceUID = series_instance_uid or generate_uid()
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
    dataset.ImageComments = _upload_comment(upload_index, upload_count)
    if instance_number is not None:
        dataset.InstanceNumber = str(instance_number)
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
    upload_index: int,
    upload_count: int,
    image: Image.Image | None = None,
    study_instance_uid: str | None = None,
    series_instance_uid: str | None = None,
    instance_number: int | None = None,
    study_description: str = "Uploaded image",
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
        study_description=study_description,
        upload_index=upload_index,
        upload_count=upload_count,
        study_instance_uid=study_instance_uid,
        series_instance_uid=series_instance_uid,
        instance_number=instance_number,
    )
    image = image or Image.open(io.BytesIO(content))
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


def _pdf_datasets(
    *,
    patient_id: str,
    patient_name: str,
    patient_birth_date: str,
    patient_sex: str,
    filename: str,
    content: bytes,
    now: datetime,
    accession_number: str,
    upload_index: int,
    upload_count: int,
) -> list[UploadDicomResult]:
    if not content.startswith(b"%PDF"):
        raise ValueError("invalid_pdf")
    images = _render_pdf_pages(content)
    if not images:
        raise ValueError("invalid_pdf")
    study_instance_uid = generate_uid()
    series_instance_uid = generate_uid()
    page_count = len(images)
    results: list[UploadDicomResult] = []
    for page_index, image in enumerate(images, start=1):
        page_accession = accession_number if page_count == 1 else f"{accession_number}{page_index:02d}"
        dataset = _image_dataset(
            patient_id=patient_id,
            patient_name=patient_name,
            patient_birth_date=patient_birth_date,
            patient_sex=patient_sex,
            filename=filename,
            content=b"",
            now=now,
            accession_number=page_accession,
            upload_index=page_index,
            upload_count=page_count,
            image=image,
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            instance_number=page_index,
            study_description="Uploaded PDF as images",
        )
        dataset.SeriesDescription = "KaosPACS PDF upload"
        dataset.ImageComments = f"Uploaded through KaosPACS Web. PDF page {page_index} of {page_count}."
        results.append(
            UploadDicomResult(
                dicom_bytes=_write_dataset(dataset),
                modality="DOC",
                study_description="Uploaded PDF as images",
                accession_number=page_accession,
            )
        )
    return results


def _render_pdf_pages(content: bytes) -> list[Image.Image]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ValueError("pdf_renderer_unavailable") from exc

    try:
        document = pdfium.PdfDocument(content)
    except Exception as exc:
        raise ValueError("invalid_pdf") from exc

    images: list[Image.Image] = []
    try:
        document_page_count = len(document)
        if document_page_count > PDF_MAX_PAGES:
            raise ValueError("pdf_too_many_pages")
        page_count = document_page_count
        for page_index in range(page_count):
            page = document[page_index]
            try:
                bitmap = page.render(scale=PDF_RENDER_SCALE)
                image = bitmap.to_pil()
                if image.mode != "RGB":
                    image = image.convert("RGB")
                images.append(image)
            finally:
                close = getattr(page, "close", None)
                if callable(close):
                    close()
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()
    return images


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


def _upload_accession_number(now: datetime, upload_index: int, upload_count: int) -> str:
    base = f"UP{now.strftime('%y%m%d%H%M%S')}"
    if upload_count <= 1:
        return base
    return f"{base}{min(max(upload_index, 1), 99):02d}"


def _upload_comment(upload_index: int, upload_count: int) -> str:
    if upload_count <= 1:
        return "Uploaded through KaosPACS Web"
    return f"Uploaded through KaosPACS Web. Upload item {upload_index} of {upload_count}."


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
