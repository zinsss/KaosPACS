from __future__ import annotations

import json
import os
from datetime import datetime
from io import BytesIO
from unittest.mock import Mock

from PIL import Image
from pydicom import dcmread
from pydicom.uid import EncapsulatedPDFStorage, SecondaryCaptureImageStorage

from app.config import load_config
from app.dicom_upload import create_upload_dicom
from app.main import make_weasis_url, render_index
from app.orthanc import StudySummary


def test_config_defaults(monkeypatch) -> None:
    for name in (
        "WEB_HTTP_HOST",
        "WEB_HTTP_PORT",
        "WEB_ORTHANC_URL",
        "WEB_ORTHANC_PUBLIC_URL",
        "WEASIS_DICOMWEB_URL",
        "WEB_STUDY_LIMIT",
        "WEB_UPLOAD_MAX_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_config()

    assert config.http_host == "0.0.0.0"
    assert config.http_port == 8081
    assert config.orthanc_url == "http://orthanc:8042"
    assert config.orthanc_public_url == "http://192.168.0.200:8042"
    assert config.weasis_dicomweb_url == "http://192.168.0.200:8042/dicom-web"
    assert config.study_limit == 100
    assert config.upload_max_bytes == 25 * 1024 * 1024


def test_config_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("WEB_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("WEB_HTTP_PORT", "18081")
    monkeypatch.setenv("WEB_ORTHANC_URL", "http://orthanc.local:8042/")
    monkeypatch.setenv("WEB_ORTHANC_PUBLIC_URL", "http://pacs:8042/")
    monkeypatch.setenv("WEASIS_DICOMWEB_URL", "http://pacs:8042/dicom-web/")
    monkeypatch.setenv("WEB_STUDY_LIMIT", "50")
    monkeypatch.setenv("WEB_UPLOAD_MAX_BYTES", "12345")

    config = load_config()

    assert config.http_host == "127.0.0.1"
    assert config.http_port == 18081
    assert config.orthanc_url == "http://orthanc.local:8042"
    assert config.orthanc_public_url == "http://pacs:8042"
    assert config.weasis_dicomweb_url == "http://pacs:8042/dicom-web"
    assert config.study_limit == 50
    assert config.upload_max_bytes == 12345


def test_weasis_url_uses_dicomweb_study_query() -> None:
    url = make_weasis_url(
        "http://192.168.0.200:8042/dicom-web",
        "1.2.3",
    )

    assert url.startswith("weasis://?")
    assert "%24dicom%3Ars" in url
    assert "studyUID%3D1.2.3" in url
    assert "192.168.0.200%3A8042%2Fdicom-web" in url


def test_render_index_escapes_values() -> None:
    config = Mock()
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    study = StudySummary(
        orthanc_id="orthanc-id",
        study_instance_uid="1.2.3",
        accession_number="<ACC>",
        patient_id="PID",
        patient_name="<b>NAME</b>",
        patient_birth_date="",
        patient_sex="",
        study_date="20260702",
        study_time="",
        study_description="<script>alert(1)</script>",
        modalities=["CR"],
        series_count=1,
        instance_count=2,
        thumbnail_instance_id="inst",
    )

    html = render_index(config, [study], query="<q>", error="")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;NAME&lt;/b&gt;" in html
    assert "2026-07-02" in html
    assert "weasis://?" in html
    assert "<script>alert(1)</script>" not in html


def test_patient_context_page_contains_upload_without_manual_patient_fields() -> None:
    config = Mock()
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"

    html = render_index(
        config,
        [],
        query="",
        patient_id="9426",
        patient_name="",
        upload_message="",
        error="",
    )

    assert "Patient/chart number" in html
    assert "9426" in html
    assert "Paste image here" in html
    assert "Nothing needs to be saved on the desktop" in html
    assert "data-paste-upload" in html
    assert 'type="file"' in html
    assert 'name="file"' in html
    assert "PatientName" not in html


def test_uploaded_png_becomes_secondary_capture_dicom() -> None:
    image = Image.new("RGB", (2, 1), color=(10, 20, 30))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    result = create_upload_dicom(
        patient_id="9426",
        patient_name="",
        filename="photo.png",
        content_type="image/png",
        content=buffer.getvalue(),
        now=datetime(2026, 7, 3, 9, 10, 11),
    )

    dataset = dcmread(BytesIO(result.dicom_bytes))
    assert dataset.SOPClassUID == SecondaryCaptureImageStorage
    assert dataset.PatientID == "9426"
    assert not hasattr(dataset, "PatientName")
    assert dataset.AccessionNumber == "UP260703091011"
    assert dataset.StudyDescription == "Uploaded image"
    assert dataset.SpecificCharacterSet == "ISO_IR 192"
    assert dataset.Rows == 1
    assert dataset.Columns == 2
    assert dataset.PixelData


def test_uploaded_pdf_becomes_encapsulated_pdf_dicom() -> None:
    result = create_upload_dicom(
        patient_id="9426",
        patient_name="",
        filename="report.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.4\nfake pdf\n%%EOF",
        now=datetime(2026, 7, 3, 9, 10, 11),
    )

    dataset = dcmread(BytesIO(result.dicom_bytes))
    assert dataset.SOPClassUID == EncapsulatedPDFStorage
    assert dataset.PatientID == "9426"
    assert dataset.AccessionNumber == "UP260703091011"
    assert dataset.StudyDescription == "Uploaded PDF"
    assert dataset.MIMETypeOfEncapsulatedDocument == "application/pdf"
    assert dataset.EncapsulatedDocument.startswith(b"%PDF")
