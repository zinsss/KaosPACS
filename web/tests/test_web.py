from __future__ import annotations

import base64
import json
import logging
import os
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

from PIL import Image
from pydicom import dcmread
from pydicom.uid import EncapsulatedPDFStorage, SecondaryCaptureImageStorage

from app.config import load_config
from app.dicom_upload import create_upload_dicom
from app.main import AIO_DISCLAIMER, create_handler, make_weasis_url, render_index
from app.orthanc import OrthancClient, StudySummary


def test_config_defaults(monkeypatch) -> None:
    for name in (
        "WEB_HTTP_HOST",
        "WEB_HTTP_PORT",
        "WEB_ORTHANC_URL",
        "WEB_ORTHANC_PUBLIC_URL",
        "WEASIS_DICOMWEB_URL",
        "KAOSPACS_AIO_URL",
        "WEB_STUDY_LIMIT",
        "WEB_UPLOAD_MAX_BYTES",
        "WEB_AUTH_USERNAME",
        "WEB_AUTH_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_config()

    assert config.http_host == "0.0.0.0"
    assert config.http_port == 8081
    assert config.orthanc_url == "http://orthanc:8042"
    assert config.orthanc_public_url == "http://192.168.0.200:8042"
    assert config.weasis_dicomweb_url == "http://192.168.0.200:8042/dicom-web"
    assert config.kaospacs_aio_url == "http://127.0.0.1:8056"
    assert config.study_limit == 100
    assert config.upload_max_bytes == 25 * 1024 * 1024
    assert config.auth_username == "kaospacs"
    assert config.auth_password == ""


def test_config_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("WEB_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("WEB_HTTP_PORT", "18081")
    monkeypatch.setenv("WEB_ORTHANC_URL", "http://orthanc.local:8042/")
    monkeypatch.setenv("WEB_ORTHANC_PUBLIC_URL", "http://pacs:8042/")
    monkeypatch.setenv("WEASIS_DICOMWEB_URL", "http://pacs:8042/dicom-web/")
    monkeypatch.setenv("KAOSPACS_AIO_URL", "http://aio:8056/")
    monkeypatch.setenv("WEB_STUDY_LIMIT", "50")
    monkeypatch.setenv("WEB_UPLOAD_MAX_BYTES", "12345")
    monkeypatch.setenv("WEB_AUTH_USERNAME", "viewer")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "secret")

    config = load_config()

    assert config.http_host == "127.0.0.1"
    assert config.http_port == 18081
    assert config.orthanc_url == "http://orthanc.local:8042"
    assert config.orthanc_public_url == "http://pacs:8042"
    assert config.weasis_dicomweb_url == "http://pacs:8042/dicom-web"
    assert config.kaospacs_aio_url == "http://aio:8056"
    assert config.study_limit == 50
    assert config.upload_max_bytes == 12345
    assert config.auth_username == "viewer"
    assert config.auth_password == "secret"


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
    assert "KaosPACS AI Opinion" in html
    assert "NOT official YHSHFM Report." in html
    assert "ONLY for AI Testing and Assistance." in html
    assert "Clinical Correlation and Physician review required." in html
    assert 'data-study-instance-uid="1.2.3"' in html
    assert 'data-orthanc-study-id="orthanc-id"' in html
    assert "No AI Opinion yet" in html
    assert "Run AI Opinion" in html
    assert "diagnosis" not in html.lower()
    assert "<script>alert(1)</script>" not in html


def test_aio_proxy_endpoints_call_aio_client() -> None:
    config = Mock()
    config.auth_password = ""
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    aio = Mock()
    aio.study_report.return_value = {
        "study_instance_uid": "1.2.3",
        "reports": [],
        "disclaimer_text": AIO_DISCLAIMER,
    }
    aio.infer.return_value = {
        "id": "report-1",
        "status": "completed",
        "disclaimer_text": AIO_DISCLAIMER,
    }
    aio.mark_reviewed.return_value = {
        "id": "report-1",
        "physician_review_status": "approved",
        "disclaimer_text": AIO_DISCLAIMER,
    }
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        create_handler(config, Mock(), aio),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = urlopen(f"{_server_url(server)}/api/aio/study/1.2.3", timeout=3)
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["disclaimer_text"] == AIO_DISCLAIMER
        aio.study_report.assert_called_once_with("1.2.3")

        infer = Request(
            f"{_server_url(server)}/api/aio/infer/orthanc-id",
            data=b"",
            method="POST",
        )
        response = urlopen(infer, timeout=3)
        assert response.status == 201
        aio.infer.assert_called_once_with("orthanc-id")

        review = Request(
            f"{_server_url(server)}/api/aio/report/report-1/review",
            data=b"",
            method="POST",
        )
        response = urlopen(review, timeout=3)
        assert response.status == 200
        aio.mark_reviewed.assert_called_once_with("report-1")
    finally:
        _stop_test_server(server, thread)


def test_web_request_logging_does_not_include_patient_query_phi(caplog) -> None:
    Handler = create_handler(Mock(), Mock())
    fake_handler = SimpleNamespace(
        command="GET",
        path="/emr.php?m_patid=CHART9426&m_patname=%EC%9D%B4%EC%A7%84%EC%84%B1&m_dob=19700101&m_sex=FEMALE",
        client_address=("10.0.0.5", 58123),
    )

    caplog.set_level(logging.INFO, logger="kaospacs.web")
    Handler.log_message(
        fake_handler,
        '"GET /emr.php?m_patid=CHART9426&m_patname=이진성&m_dob=19700101&m_sex=FEMALE HTTP/1.1" 200 -',
    )

    log_text = caplog.text
    assert "method=GET" in log_text
    assert "path=/emr.php" in log_text
    assert "client_ip=10.0.0.5" in log_text
    assert "m_patid" not in log_text
    assert "CHART9426" not in log_text
    assert "m_patname" not in log_text
    assert "이진성" not in log_text
    assert "m_dob" not in log_text
    assert "19700101" not in log_text
    assert "m_sex" not in log_text
    assert "FEMALE" not in log_text


def test_web_health_does_not_require_auth() -> None:
    config = Mock()
    config.auth_username = "kaospacs"
    config.auth_password = "secret"
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    server, thread = _start_test_server(config, Mock())
    try:
        response = urlopen(f"{_server_url(server)}/health", timeout=3)
        assert response.status == 200
    finally:
        _stop_test_server(server, thread)


def test_web_protected_page_requires_auth_when_password_configured(caplog) -> None:
    config = Mock()
    config.auth_username = "kaospacs"
    config.auth_password = "secret"
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    orthanc = Mock()
    orthanc.studies_for_patient.return_value = []
    server, thread = _start_test_server(config, orthanc)
    try:
        url = (
            f"{_server_url(server)}/emr.php?"
            "m_patid=CHART9426&m_patname=%EC%9D%B4%EC%A7%84%EC%84%B1"
            "&m_dob=19700101&m_sex=FEMALE"
        )
        caplog.set_level(logging.INFO, logger="kaospacs.web")
        try:
            urlopen(url, timeout=3)
            raise AssertionError("expected unauthorized response")
        except HTTPError as exc:
            assert exc.code == 401

        log_text = caplog.text
        assert "authentication failed" in log_text
        assert "CHART9426" not in log_text
        assert "이진성" not in log_text
        assert "19700101" not in log_text
        assert "FEMALE" not in log_text
        assert "Authorization" not in log_text
        assert "secret" not in log_text
    finally:
        _stop_test_server(server, thread)


def test_web_protected_page_allows_correct_basic_auth(caplog) -> None:
    config = Mock()
    config.auth_username = "kaospacs"
    config.auth_password = "secret"
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    orthanc = Mock()
    orthanc.studies_for_patient.return_value = []
    server, thread = _start_test_server(config, orthanc)
    try:
        credentials = base64.b64encode(b"kaospacs:secret").decode("ascii")
        request = Request(
            f"{_server_url(server)}/emr.php?m_patid=CHART9426",
            headers={"Authorization": f"Basic {credentials}"},
        )
        caplog.set_level(logging.INFO, logger="kaospacs.web")
        response = urlopen(request, timeout=3)

        assert response.status == 200
        assert orthanc.studies_for_patient.called
        assert "secret" not in caplog.text
        assert "Authorization" not in caplog.text
    finally:
        _stop_test_server(server, thread)


def test_patient_context_page_contains_upload_without_manual_patient_fields() -> None:
    config = Mock()
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"

    html = render_index(
        config,
        [],
        query="",
        patient_id="9426",
        patient_name="이진성",
        patient_birth_date="19700101",
        patient_sex="M",
        upload_message="",
        error="",
    )

    assert "Chart no." in html
    assert "9426" in html
    assert "Name" in html
    assert "이진성" in html
    assert "DOB" in html
    assert "19700101" in html
    assert "Sex" in html
    assert "M" in html
    assert "Paste image here" in html
    assert "Nothing needs to be saved on the desktop" in html
    assert "data-paste-upload" in html
    assert 'id="paste-queue"' in html
    assert 'id="paste-clear"' in html
    assert "Clear all" in html
    assert "Move up" in html
    assert "Move down" in html
    assert "Remove" in html
    assert 'type="file"' in html
    assert 'name="file"' in html
    assert "multiple" in html
    assert 'name="patient_name"' not in html
    assert 'name="dob"' not in html
    assert 'name="sex"' not in html


def test_patient_context_fills_missing_demographics_from_studies() -> None:
    config = Mock()
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    study = StudySummary(
        orthanc_id="orthanc-id",
        study_instance_uid="1.2.3",
        accession_number="ACC",
        patient_id="9426",
        patient_name="이진성",
        patient_birth_date="19700101",
        patient_sex="M",
        study_date="20260708",
        study_time="",
        study_description="흉부",
        modalities=["CR"],
        series_count=1,
        instance_count=1,
        thumbnail_instance_id="inst",
    )

    html = render_index(
        config,
        [study],
        query="",
        patient_id="9426",
        patient_name="",
        patient_birth_date="",
        patient_sex="",
        upload_message="",
        error="",
    )

    assert "이진성" in html
    assert "19700101" in html
    assert "M" in html
    assert "m_patname=%EC%9D%B4%EC%A7%84%EC%84%B1" in html
    assert "m_dob=19700101" in html
    assert "m_sex=M" in html


def test_study_card_shows_dob_and_sex() -> None:
    config = Mock()
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    study = StudySummary(
        orthanc_id="orthanc-id",
        study_instance_uid="1.2.3",
        accession_number="ACC",
        patient_id="9426",
        patient_name="이진성",
        patient_birth_date="19700101",
        patient_sex="M",
        study_date="20260708",
        study_time="",
        study_description="흉부",
        modalities=["CR"],
        series_count=1,
        instance_count=1,
        thumbnail_instance_id="inst",
    )

    html = render_index(
        config,
        [study],
        query="",
        patient_id="9426",
        upload_message="",
        error="",
    )

    assert "<dt>DOB</dt><dd>19700101</dd>" in html
    assert "<dt>Sex</dt><dd>M</dd>" in html


def test_orthanc_summary_falls_back_to_instance_patient_tags() -> None:
    client = OrthancClient("http://orthanc")
    payloads = {
        "/series/series-1": {
            "MainDicomTags": {"Modality": "CR"},
            "Instances": ["instance-1"],
        },
        "/instances/instance-1/simplified-tags": {
            "PatientID": "9426",
            "PatientName": "이진성",
            "PatientBirthDate": "19700101",
            "PatientSex": "M",
        },
    }
    client._json = Mock(side_effect=lambda path, params=None: payloads[path])
    study = {
        "ID": "study-1",
        "MainDicomTags": {
            "StudyInstanceUID": "1.2.3",
            "AccessionNumber": "ACC",
        },
        "PatientMainDicomTags": {"PatientID": "9426"},
        "Series": ["series-1"],
    }

    summary = client._summary(study)

    assert summary.patient_id == "9426"
    assert summary.patient_name == "이진성"
    assert summary.patient_birth_date == "19700101"
    assert summary.patient_sex == "M"


def test_web_upload_accepts_single_file_field() -> None:
    config = Mock()
    config.auth_password = ""
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    config.upload_max_bytes = 25 * 1024 * 1024
    config.study_limit = 100
    orthanc = Mock()
    orthanc.studies_for_patient.return_value = []
    server, thread = _start_test_server(config, orthanc)
    try:
        body, content_type = _multipart_body(
            [("file", "single.png", "image/png", _png_bytes((20, 30, 40)))]
        )
        request = Request(
            f"{_server_url(server)}/emr.php?m_patid=9426&m_patname=%EC%9D%B4%EC%A7%84%EC%84%B1&m_dob=19700101&m_sex=M",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        response = urlopen(request, timeout=3)

        assert response.status == 200
        assert orthanc.upload_instance.call_count == 1
        dataset = dcmread(BytesIO(orthanc.upload_instance.call_args.args[0]))
        assert dataset.SOPClassUID == SecondaryCaptureImageStorage
        assert dataset.PatientID == "9426"
        assert str(dataset.PatientName) == "이진성"
        assert dataset.PatientBirthDate == "19700101"
        assert dataset.PatientSex == "M"
        assert dataset.SpecificCharacterSet == "ISO_IR 192"
        assert dataset.ImageComments == "Uploaded through KaosPACS Web"
    finally:
        _stop_test_server(server, thread)


def test_web_upload_accepts_multiple_file_fields_and_logs_no_phi(caplog) -> None:
    config = Mock()
    config.auth_password = ""
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    config.upload_max_bytes = 25 * 1024 * 1024
    config.study_limit = 100
    orthanc = Mock()
    orthanc.studies_for_patient.return_value = []
    server, thread = _start_test_server(config, orthanc)
    try:
        body, content_type = _multipart_body(
            [
                ("file", "pasted-image-01.png", "image/png", _png_bytes((10, 20, 30))),
                ("file", "pasted-image-02.png", "image/png", _png_bytes((40, 50, 60))),
            ]
        )
        request = Request(
            f"{_server_url(server)}/emr.php?m_patid=CHART9426&m_patname=%EC%9D%B4%EC%A7%84%EC%84%B1&m_dob=19700101&m_sex=FEMALE",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        caplog.set_level(logging.INFO, logger="kaospacs.web")
        response = urlopen(request, timeout=3)

        assert response.status == 200
        assert orthanc.upload_instance.call_count == 2
        datasets = [
            dcmread(BytesIO(call.args[0]))
            for call in orthanc.upload_instance.call_args_list
        ]
        assert all(dataset.SOPClassUID == SecondaryCaptureImageStorage for dataset in datasets)
        assert all(dataset.PatientID == "CHART9426" for dataset in datasets)
        assert all(str(dataset.PatientName) == "이진성" for dataset in datasets)
        assert all(dataset.PatientBirthDate == "19700101" for dataset in datasets)
        assert all(dataset.PatientSex == "F" for dataset in datasets)
        assert all(dataset.SpecificCharacterSet == "ISO_IR 192" for dataset in datasets)
        assert datasets[0].ImageComments.endswith("Upload item 1 of 2.")
        assert datasets[1].ImageComments.endswith("Upload item 2 of 2.")
        assert datasets[0].AccessionNumber != datasets[1].AccessionNumber

        log_text = caplog.text
        assert "uploaded_count=2" in log_text
        assert "failed_count=0" in log_text
        assert "CHART9426" not in log_text
        assert "이진성" not in log_text
        assert "19700101" not in log_text
        assert "FEMALE" not in log_text
        assert "pasted-image" not in log_text
    finally:
        _stop_test_server(server, thread)


def test_web_upload_post_requires_auth_when_password_configured() -> None:
    config = Mock()
    config.auth_username = "kaospacs"
    config.auth_password = "secret"
    config.weasis_dicomweb_url = "http://pacs/dicom-web"
    config.orthanc_public_url = "http://pacs"
    config.upload_max_bytes = 25 * 1024 * 1024
    orthanc = Mock()
    server, thread = _start_test_server(config, orthanc)
    try:
        body, content_type = _multipart_body(
            [("file", "single.png", "image/png", _png_bytes((20, 30, 40)))]
        )
        request = Request(
            f"{_server_url(server)}/emr.php?m_patid=9426",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
            raise AssertionError("expected unauthorized response")
        except HTTPError as exc:
            assert exc.code == 401
        assert not orthanc.upload_instance.called
    finally:
        _stop_test_server(server, thread)


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


def test_uploaded_png_can_include_patient_context_demographics() -> None:
    image = Image.new("RGB", (2, 1), color=(10, 20, 30))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    result = create_upload_dicom(
        patient_id="9426",
        patient_name="이진성",
        patient_birth_date="1970-01-01",
        patient_sex="male",
        filename="photo.png",
        content_type="image/png",
        content=buffer.getvalue(),
        now=datetime(2026, 7, 3, 9, 10, 11),
    )

    dataset = dcmread(BytesIO(result.dicom_bytes))
    assert dataset.SpecificCharacterSet == "ISO_IR 192"
    assert dataset.PatientID == "9426"
    assert str(dataset.PatientName) == "이진성"
    assert dataset.PatientBirthDate == "19700101"
    assert dataset.PatientSex == "M"


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


def _start_test_server(config: Mock, orthanc: Mock) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(config, orthanc))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_test_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _server_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (2, 1), color=color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _multipart_body(
    files: list[tuple[str, str, str, bytes]],
) -> tuple[bytes, str]:
    boundary = "----kaospacs-test-boundary"
    chunks: list[bytes] = []
    for field_name, filename, content_type, content in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("ascii"),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
