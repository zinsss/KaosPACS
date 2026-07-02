from __future__ import annotations

import re
import uuid
from pathlib import Path

from pydicom.dataset import Dataset
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian, PYDICOM_IMPLEMENTATION_UID


SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_dicom_filename(dataset: Dataset) -> str:
    raw_value = str(getattr(dataset, "SOPInstanceUID", "") or uuid.uuid4())
    safe_value = SAFE_FILENAME_PATTERN.sub("_", raw_value).strip("._")
    if not safe_value:
        safe_value = str(uuid.uuid4())
    return f"{safe_value}.dcm"


def dicom_storage_path(storage_dir: Path, dataset: Dataset) -> Path:
    return storage_dir / safe_dicom_filename(dataset)


def store_dataset(dataset: Dataset, storage_dir: Path) -> Path:
    storage_dir.mkdir(parents=True, exist_ok=True)
    path = dicom_storage_path(storage_dir, dataset)
    ensure_file_meta(dataset)
    dataset.save_as(path, write_like_original=False)
    return path


def ensure_file_meta(dataset: Dataset) -> None:
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is None:
        file_meta = FileMetaDataset()
        dataset.file_meta = file_meta

    if "MediaStorageSOPClassUID" not in file_meta and getattr(dataset, "SOPClassUID", None):
        file_meta.MediaStorageSOPClassUID = dataset.SOPClassUID
    if "MediaStorageSOPInstanceUID" not in file_meta and getattr(dataset, "SOPInstanceUID", None):
        file_meta.MediaStorageSOPInstanceUID = dataset.SOPInstanceUID
    if "TransferSyntaxUID" not in file_meta:
        if getattr(dataset, "is_implicit_VR", False):
            file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
        else:
            file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    if "ImplementationClassUID" not in file_meta:
        file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
