from __future__ import annotations

import re
import uuid
from pathlib import Path

from pydicom.dataset import Dataset


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
    dataset.save_as(path, write_like_original=False)
    return path
