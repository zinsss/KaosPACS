from __future__ import annotations

import base64
import binascii
import hmac
import html
import json
import logging
import warnings
from dataclasses import asdict, dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlparse
from urllib.request import Request, urlopen

from .config import Config, load_config
from .dicom_upload import create_upload_dicom
from .gateway import GatewayMetadataClient
from .orthanc import OrthancClient, StudySummary

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import cgi


LOGGER = logging.getLogger("kaospacs.web")

AIO_DISCLAIMER = (
    "KaosPACS AI Opinion\n\n"
    "NOT official YHSHFM Report.\n"
    "ONLY for AI Testing and Assistance.\n"
    "Clinical Correlation and Physician review required."
)


@dataclass(frozen=True)
class PatientContext:
    patient_id: str = ""
    patient_name: str = ""
    patient_birth_date: str = ""
    patient_sex: str = ""


@dataclass(frozen=True)
class UploadSummary:
    uploaded_count: int = 0
    failed_count: int = 0
    first_error: str = ""


class AioClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def study_report(self, study_instance_uid: str) -> dict[str, Any]:
        return self._json("GET", f"/api/aio/study/{quote(study_instance_uid, safe='')}")

    def infer(self, orthanc_study_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/aio/infer/{quote(orthanc_study_id, safe='')}")

    def mark_reviewed(self, report_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/aio/report/{quote(report_id, safe='')}/review",
            {
                "physician_review_status": "approved",
                "reviewed_by": "KaosPACS-Web",
            },
        )

    def _json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def make_weasis_url(dicomweb_url: str, study_instance_uid: str) -> str:
    command = (
        f'$dicom:rs --url "{dicomweb_url.rstrip("/")}" '
        f'-r "studyUID={study_instance_uid}"'
    )
    return "weasis://?" + quote_plus(command)


def create_handler(
    config: Config,
    orthanc: OrthancClient,
    aio: AioClient | None = None,
    gateway_metadata: GatewayMetadataClient | None = None,
) -> type[BaseHTTPRequestHandler]:
    aio_client = aio or AioClient(config.kaospacs_aio_url)
    metadata_client = gateway_metadata or GatewayMetadataClient(
        getattr(config, "gateway_url", "http://gateway:8060"),
        token=getattr(config, "gateway_api_token", ""),
        timeout_seconds=getattr(config, "gateway_timeout_seconds", 3.0),
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "KaosPACSWeb/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json({"status": "ok", "service": "web", "version": "0.1"})
                return
            if not self._require_auth(parsed.path):
                return
            if parsed.path == "/api/studies":
                self._api_studies(parsed.query)
                return
            if parsed.path.startswith("/api/aio/study/"):
                self._api_aio_study(parsed.path.removeprefix("/api/aio/study/"))
                return
            if parsed.path.startswith("/thumbnail/"):
                self._thumbnail(parsed.path.removeprefix("/thumbnail/"))
                return
            if parsed.path in ("/", "/emr.php"):
                self._index(parsed.path, parsed.query)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not self._require_auth(parsed.path):
                return
            if parsed.path == "/emr.php":
                self._upload(parsed.query)
                return
            if parsed.path.startswith("/api/aio/infer/"):
                self._api_aio_infer(parsed.path.removeprefix("/api/aio/infer/"))
                return
            if parsed.path.startswith("/api/aio/report/") and parsed.path.endswith("/review"):
                report_id = parsed.path.removeprefix("/api/aio/report/").removesuffix("/review")
                self._api_aio_review(report_id)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:
            parsed = urlparse(getattr(self, "path", ""))
            client_ip = "-"
            client_address = getattr(self, "client_address", None)
            if client_address:
                client_ip = str(client_address[0])
            LOGGER.info(
                "Web request method=%s path=%s client_ip=%s",
                getattr(self, "command", "-"),
                parsed.path or "-",
                client_ip,
            )

        def _require_auth(self, path: str) -> bool:
            if not config.auth_password:
                return True
            if _check_basic_auth(
                self.headers.get("Authorization", ""),
                config.auth_username,
                config.auth_password,
            ):
                LOGGER.info(
                    "Web authentication success path=%s client_ip=%s",
                    path or "-",
                    self.client_address[0] if self.client_address else "-",
                )
                return True
            LOGGER.warning(
                "Web authentication failed path=%s client_ip=%s",
                path or "-",
                self.client_address[0] if self.client_address else "-",
            )
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="KaosPACS Web"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = b'{"error":"unauthorized"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def _api_studies(self, query_string: str) -> None:
            params = parse_qs(query_string)
            query = params.get("q", [""])[0]
            limit = _safe_limit(params.get("limit", [str(config.study_limit)])[0])
            try:
                studies = _enrich_studies_with_operational_metadata(
                    orthanc.studies(query=query, limit=limit),
                    metadata_client,
                )
            except Exception as exc:
                LOGGER.warning("Orthanc study query failed exception=%s", exc.__class__.__name__)
                self._json({"error": "orthanc_unavailable"}, HTTPStatus.BAD_GATEWAY)
                return
            self._json(
                {
                    "entries": [_study_payload(study, config) for study in studies],
                    "count": len(studies),
                }
            )

        def _api_aio_study(self, study_instance_uid: str) -> None:
            if not study_instance_uid:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                self._json(aio_client.study_report(study_instance_uid))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                LOGGER.warning("AIO study lookup failed exception=%s", exc.__class__.__name__)
                self._json({"error": "aio_unavailable"}, HTTPStatus.BAD_GATEWAY)

        def _api_aio_infer(self, orthanc_study_id: str) -> None:
            if not orthanc_study_id:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                self._json(aio_client.infer(orthanc_study_id), HTTPStatus.CREATED)
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                LOGGER.warning("AIO infer failed exception=%s", exc.__class__.__name__)
                self._json({"error": "aio_unavailable"}, HTTPStatus.BAD_GATEWAY)

        def _api_aio_review(self, report_id: str) -> None:
            if not report_id:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                self._json(aio_client.mark_reviewed(report_id))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                LOGGER.warning("AIO review failed exception=%s", exc.__class__.__name__)
                self._json({"error": "aio_unavailable"}, HTTPStatus.BAD_GATEWAY)

        def _thumbnail(self, instance_id: str) -> None:
            if not instance_id:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                body, content_type = orthanc.preview(instance_id)
            except Exception as exc:
                LOGGER.warning("Orthanc thumbnail failed exception=%s", exc.__class__.__name__)
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "private, max-age=60")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _index(self, path: str, query_string: str) -> None:
            params = parse_qs(query_string)
            query = params.get("q", [""])[0]
            patient = _patient_context_from_params(params)
            upload_status = params.get("upload", [""])[0]
            upload_message = _upload_status_message(
                upload_status,
                params.get("uploaded_count", [""])[0],
                params.get("failed_count", [""])[0],
            )
            try:
                if path == "/emr.php" and patient.patient_id:
                    studies = _enrich_studies_with_operational_metadata(
                        orthanc.studies_for_patient(
                            patient.patient_id,
                            query=query,
                            limit=config.study_limit,
                        ),
                        metadata_client,
                    )
                elif path == "/emr.php":
                    studies = []
                else:
                    studies = _enrich_studies_with_operational_metadata(
                        orthanc.studies(query=query, limit=config.study_limit),
                        metadata_client,
                    )
                body = render_index(
                    config,
                    studies,
                    query=query,
                    patient_id=patient.patient_id,
                    patient_name=patient.patient_name,
                    patient_birth_date=patient.patient_birth_date,
                    patient_sex=patient.patient_sex,
                    upload_message=upload_message,
                    error="",
                )
            except Exception as exc:
                LOGGER.warning("Orthanc page query failed exception=%s", exc.__class__.__name__)
                body = render_index(
                    config,
                    [],
                    query=query,
                    patient_id=patient.patient_id,
                    patient_name=patient.patient_name,
                    patient_birth_date=patient.patient_birth_date,
                    patient_sex=patient.patient_sex,
                    upload_message="",
                    error="Orthanc is not reachable.",
                )
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _upload(self, query_string: str) -> None:
            params = parse_qs(query_string)
            patient = _patient_context_from_params(params)
            if not patient.patient_id:
                self._redirect_upload(params, "missing_patient")
                return

            content_length = int(self.headers.get("Content-Length") or "0")
            if content_length <= 0:
                self._redirect_upload(params, "missing_file")
                return
            if content_length > config.upload_max_bytes:
                self._redirect_upload(params, "too_large")
                return

            try:
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                        "CONTENT_LENGTH": str(content_length),
                    },
                )
                field = form["file"] if "file" in form else None
                fields = _file_fields(field)
                if not fields:
                    self._redirect_upload(params, "missing_file")
                    return
                summary = self._store_upload_fields(patient, fields)
            except ValueError as exc:
                LOGGER.info(
                    "Web upload rejected event=upload_rejected reason=%s",
                    str(exc),
                )
                self._redirect_upload(params, str(exc))
                return
            except Exception as exc:
                LOGGER.warning(
                    "Web upload failed event=upload_failed exception=%s",
                    exc.__class__.__name__,
                )
                self._redirect_upload(params, "failed")
                return

            LOGGER.info(
                "Web upload batch complete event=upload_batch_complete uploaded_count=%s failed_count=%s",
                summary.uploaded_count,
                summary.failed_count,
            )
            self._redirect_upload(
                params,
                _upload_redirect_status(summary),
                summary.uploaded_count,
                summary.failed_count,
            )

        def _store_upload_fields(
            self,
            patient: PatientContext,
            fields: list[cgi.FieldStorage],
        ) -> UploadSummary:
            uploaded_count = 0
            failed_count = 0
            first_error = ""
            upload_count = len(fields)
            for index, field in enumerate(fields, start=1):
                try:
                    content = field.file.read(config.upload_max_bytes + 1)
                    if not content:
                        raise ValueError("missing_file")
                    if len(content) > config.upload_max_bytes:
                        raise ValueError("too_large")
                    result = create_upload_dicom(
                        patient_id=patient.patient_id,
                        patient_name=patient.patient_name,
                        patient_birth_date=patient.patient_birth_date,
                        patient_sex=patient.patient_sex,
                        filename=getattr(field, "filename", "") or f"upload-{index}.png",
                        content_type=getattr(field, "type", "") or "",
                        content=content,
                        upload_index=index,
                        upload_count=upload_count,
                    )
                    orthanc.upload_instance(result.dicom_bytes)
                    uploaded_count += 1
                    LOGGER.info(
                        "Web upload stored event=upload_stored accession_number=%s modality=%s upload_index=%s upload_count=%s",
                        result.accession_number,
                        result.modality,
                        index,
                        upload_count,
                    )
                except ValueError as exc:
                    failed_count += 1
                    first_error = first_error or str(exc)
                    LOGGER.info(
                        "Web upload item rejected event=upload_item_rejected reason=%s upload_index=%s upload_count=%s",
                        str(exc),
                        index,
                        upload_count,
                    )
                except Exception as exc:
                    failed_count += 1
                    first_error = first_error or "failed"
                    LOGGER.warning(
                        "Web upload item failed event=upload_item_failed exception=%s upload_index=%s upload_count=%s",
                        exc.__class__.__name__,
                        index,
                        upload_count,
                    )
            return UploadSummary(uploaded_count, failed_count, first_error)

        def _redirect_upload(
            self,
            params: dict[str, list[str]],
            status: str,
            uploaded_count: int = 0,
            failed_count: int = 0,
        ) -> None:
            query = {key: values[0] for key, values in params.items() if values}
            query["upload"] = status
            if uploaded_count:
                query["uploaded_count"] = str(uploaded_count)
            if failed_count:
                query["failed_count"] = str(failed_count)
            location = "/emr.php?" + urlencode(query)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def render_index(
    config: Config,
    studies: list[StudySummary],
    *,
    query: str,
    patient_id: str = "",
    patient_name: str = "",
    patient_birth_date: str = "",
    patient_sex: str = "",
    upload_message: str = "",
    error: str,
) -> str:
    rows = "\n".join(_study_card(config, study) for study in studies)
    if not rows and not error and patient_id:
        rows = (
            '<div class="empty">No Orthanc studies were found for this patient.</div>'
        )
    elif not rows and not error:
        rows = '<div class="empty">No studies found in Orthanc.</div>'
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    patient_html = _patient_context_html(
        PatientContext(
            patient_id=patient_id,
            patient_name=patient_name,
            patient_birth_date=patient_birth_date,
            patient_sex=patient_sex,
        )
    )
    upload_html = (
        _upload_form(
            PatientContext(
                patient_id=patient_id,
                patient_name=patient_name,
                patient_birth_date=patient_birth_date,
                patient_sex=patient_sex,
            ),
            query,
            upload_message,
        )
        if patient_id
        else '<div class="notice">No patient/chart number was provided in m_patid.</div>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KaosPACS Studies</title>
  <style>{CSS}</style>
</head>
<body>
  <header class="topbar">
    <div>
      <h1>KaosPACS Studies</h1>
      <p>Orthanc archive with Weasis launch links</p>
    </div>
    <form method="get" action="/" class="search">
      <input name="q" value="{html.escape(query)}" placeholder="Search patient, accession, modality">
      <button type="submit">Search</button>
    </form>
  </header>
  <main>
    {error_html}
    {patient_html}
    {upload_html}
    <section class="summary">{len(studies)} studies</section>
    <section class="grid">{rows}</section>
</main>
<script>{AIO_PANEL_SCRIPT}</script>
</body>
</html>"""


def _study_card(config: Config, study: StudySummary) -> str:
    thumbnail = (
        f'<img src="/thumbnail/{html.escape(study.thumbnail_instance_id)}" alt="">'
        if study.thumbnail_instance_id
        else '<div class="no-thumb">No preview</div>'
    )
    weasis_url = make_weasis_url(config.weasis_dicomweb_url, study.study_instance_uid)
    orthanc_url = f"{config.orthanc_public_url}/ui/app/"
    modality = ", ".join(study.modalities) or study.operational_display_modality or "-"
    date = _format_date(study.study_date)
    description = study.study_description or "No study description"
    return f"""
<article class="study">
  <div class="thumb">{thumbnail}</div>
  <div class="study-body">
    <div class="line">
      <span class="modality">{html.escape(modality)}</span>
      <span class="date">{html.escape(date)}</span>
    </div>
    <h2>{html.escape(description)}</h2>
    <dl>
      <div><dt>Patient</dt><dd>{html.escape(study.patient_name or "-")}</dd></div>
      <div><dt>ID</dt><dd>{html.escape(study.patient_id or "-")}</dd></div>
      <div><dt>Accession</dt><dd>{html.escape(study.accession_number or "-")}</dd></div>
      <div><dt>Series</dt><dd>{study.series_count} / {study.instance_count} images</dd></div>
    </dl>
    <div class="actions">
      <a class="primary" href="{html.escape(weasis_url)}">Open with Weasis</a>
      <a href="{html.escape(orthanc_url)}" target="_blank" rel="noreferrer">Orthanc</a>
    </div>
    {_aio_panel(study)}
  </div>
</article>"""


def _enrich_studies_with_operational_metadata(
    studies: list[StudySummary],
    metadata_client: GatewayMetadataClient,
) -> list[StudySummary]:
    enriched: list[StudySummary] = []
    for study in studies:
        if study.modalities:
            enriched.append(study)
            continue
        metadata = metadata_client.get_by_study(study.orthanc_id)
        if metadata is None:
            metadata = metadata_client.get_by_accession(study.accession_number)
        if metadata is None:
            enriched.append(study)
            continue
        enriched.append(
            replace(
                study,
                operational_display_modality=metadata.display_modality,
            )
        )
    return enriched


def _aio_panel(study: StudySummary) -> str:
    return f"""
    <section class="aio-panel"
      data-aio-panel
      data-study-instance-uid="{html.escape(study.study_instance_uid, quote=True)}"
      data-orthanc-study-id="{html.escape(study.orthanc_id, quote=True)}">
      <h3>KaosPACS AI Opinion</h3>
      <pre class="aio-disclaimer">{html.escape(AIO_DISCLAIMER)}</pre>
      <div class="aio-content" aria-live="polite">
        <p>No AI Opinion yet</p>
        <button type="button" data-aio-run>Run AI Opinion</button>
      </div>
    </section>"""


def _upload_form(
    patient: PatientContext,
    query: str,
    upload_message: str,
) -> str:
    query_params = {"m_patid": patient.patient_id}
    if patient.patient_name:
        query_params["m_patname"] = patient.patient_name
    if patient.patient_birth_date:
        query_params["m_dob"] = patient.patient_birth_date
    if patient.patient_sex:
        query_params["m_sex"] = patient.patient_sex
    if query:
        query_params["q"] = query
    action = "/emr.php?" + urlencode(query_params)
    message = (
        f'<div class="upload-message">{html.escape(upload_message)}</div>'
        if upload_message
        else ""
    )
    return f"""
<section class="upload-panel">
  <form method="post" action="{html.escape(action)}" enctype="multipart/form-data" data-paste-upload>
    <label for="file">Add image or PDF to this patient's PACS</label>
    <div id="paste-zone" class="paste-zone" tabindex="0">
      <strong>Paste image here</strong>
      <span>Copy one or more images or screenshots, click here, then press Ctrl+V. Nothing needs to be saved on the desktop.</span>
      <div id="paste-status" class="paste-status" aria-live="polite"></div>
    </div>
    <div class="paste-queue-bar">
      <strong>Queued pasted images</strong>
      <button type="button" id="paste-clear" class="secondary">Clear all</button>
    </div>
    <div id="paste-queue" class="paste-queue" aria-live="polite"></div>
    <div class="upload-row">
      <input id="file" name="file" type="file" accept="image/jpeg,image/png,application/pdf" multiple required>
      <button type="submit">Upload</button>
    </div>
    <p>Paste one or more images, or choose JPG, PNG, or PDF. Each queued pasted image is stored as a separate DICOM in Orthanc for PatientID {html.escape(patient.patient_id)}. PDF upload remains file-picker only.</p>
  </form>
  {message}
</section>
<script>{PASTE_SCRIPT}</script>"""


def _study_payload(study: StudySummary, config: Config) -> dict[str, Any]:
    payload = asdict(study)
    payload["thumbnail_url"] = (
        f"/thumbnail/{study.thumbnail_instance_id}" if study.thumbnail_instance_id else ""
    )
    payload["weasis_url"] = make_weasis_url(
        config.weasis_dicomweb_url,
        study.study_instance_uid,
    )
    return payload


def _format_date(value: str) -> str:
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value or "-"


def _safe_limit(raw: str) -> int:
    try:
        return min(max(int(raw), 1), 500)
    except ValueError:
        return 100


def _patient_context_html(patient: PatientContext) -> str:
    if not patient.patient_id:
        return ""
    items = (
        ("Chart no.", patient.patient_id),
        ("Name", patient.patient_name),
        ("DOB", patient.patient_birth_date),
        ("Sex", patient.patient_sex),
    )
    fields = "\n".join(
        "<div>"
        f"<span>{html.escape(label)}</span>"
        f"<strong>{html.escape(value or '-')}</strong>"
        "</div>"
        for label, value in items
    )
    return f'<section class="patient-context">{fields}</section>'


def _patient_context_from_params(params: dict[str, list[str]]) -> PatientContext:
    return PatientContext(
        patient_id=_first_param(params, ("m_patid", "patient_id", "PatientID")),
        patient_name=_first_param(
            params,
            ("m_patname", "m_name", "patient_name", "PatientName"),
        ),
        patient_birth_date=_first_param(
            params,
            (
                "m_dob",
                "m_patbirth",
                "m_birthday",
                "patient_birth_date",
                "PatientBirthDate",
                "dob",
            ),
        ),
        patient_sex=_first_param(params, ("m_sex", "patient_sex", "PatientSex", "sex")),
    )


def _first_param(params: dict[str, list[str]], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = params.get(key, [""])[0].strip()
        if value:
            return value
    return ""


def _file_fields(field: Any) -> list[cgi.FieldStorage]:
    fields = field if isinstance(field, list) else [field]
    return [
        item
        for item in fields
        if item is not None and getattr(item, "filename", "") and getattr(item, "file", None)
    ]


def _check_basic_auth(header: str, expected_username: str, expected_password: str) -> bool:
    if not expected_password or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic "), validate=True).decode(
            "utf-8"
        )
    except (binascii.Error, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(
        password,
        expected_password,
    )


def _upload_redirect_status(summary: UploadSummary) -> str:
    if summary.uploaded_count and summary.failed_count:
        return "partial"
    if summary.uploaded_count:
        return "success"
    return summary.first_error or "failed"


def _upload_status_message(status: str, uploaded_count: str = "", failed_count: str = "") -> str:
    uploaded = _safe_count(uploaded_count)
    failed = _safe_count(failed_count)
    if status == "success" and uploaded > 1:
        return f"Upload added {uploaded} items to PACS."
    if status == "partial":
        return f"Upload added {uploaded} items to PACS; {failed} items failed."
    messages = {
        "success": "Upload added to PACS.",
        "missing_patient": "No patient/chart number was provided.",
        "missing_file": "Choose a JPG, PNG, or PDF file to upload.",
        "too_large": "The selected file is too large.",
        "unsupported_upload_type": "Only JPG, PNG, and PDF files are supported.",
        "invalid_pdf": "The uploaded PDF file is not valid.",
        "failed": "Upload failed while saving to PACS.",
    }
    return messages.get(status, "")


def _safe_count(raw: str) -> int:
    try:
        return max(int(raw), 0)
    except ValueError:
        return 0


CSS = """
:root { color-scheme: light; --border:#d8dee8; --text:#152033; --muted:#5f6c7b; --panel:#fff; --bg:#f5f7fa; --blue:#175cd3; }
* { box-sizing:border-box; }
body { margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
.topbar { display:flex; gap:24px; align-items:end; justify-content:space-between; padding:24px 28px 18px; border-bottom:1px solid var(--border); background:#fff; }
h1 { margin:0; font-size:24px; line-height:1.2; font-weight:700; letter-spacing:0; }
p { margin:5px 0 0; color:var(--muted); }
.search { display:flex; gap:8px; min-width:min(520px, 100%); }
input { width:100%; min-height:40px; padding:8px 11px; border:1px solid var(--border); border-radius:6px; font:inherit; }
button, a { min-height:36px; border-radius:6px; border:1px solid var(--border); padding:8px 11px; background:#fff; color:var(--text); text-decoration:none; font:inherit; display:inline-flex; align-items:center; justify-content:center; white-space:nowrap; }
button, .primary { background:var(--blue); color:#fff; border-color:var(--blue); }
main { padding:18px 28px 32px; }
.summary { color:var(--muted); margin-bottom:12px; }
.patient-context, .upload-panel { background:#fff; border:1px solid var(--border); border-radius:8px; padding:12px 14px; margin-bottom:12px; }
.patient-context { display:grid; grid-template-columns:repeat(4, minmax(120px, 1fr)); gap:10px 14px; }
.patient-context span { display:block; color:var(--muted); font-size:12px; margin-bottom:2px; }
.patient-context strong { display:block; overflow-wrap:anywhere; }
.upload-panel label { display:block; font-weight:700; margin-bottom:8px; }
.upload-row { display:flex; gap:8px; align-items:center; }
.upload-row input { min-height:36px; }
.upload-panel p { font-size:13px; }
.upload-message { margin-top:8px; color:#0f766e; font-weight:700; }
.paste-zone { border:1px dashed #9aa8b8; border-radius:8px; padding:12px; margin:8px 0 10px; background:#f8fafc; outline:none; }
.paste-zone:focus { border-color:var(--blue); box-shadow:0 0 0 3px rgba(23,92,211,.12); }
.paste-zone strong { display:block; margin-bottom:4px; }
.paste-zone span, .paste-status { color:var(--muted); font-size:13px; }
.paste-queue-bar { display:flex; align-items:center; justify-content:space-between; gap:10px; margin:8px 0; }
.paste-queue-bar strong { font-size:14px; }
.paste-queue { display:grid; grid-template-columns:repeat(auto-fill, minmax(180px, 1fr)); gap:10px; margin-bottom:10px; }
.paste-item { border:1px solid var(--border); border-radius:8px; background:#f8fafc; padding:8px; }
.paste-item img { display:block; width:100%; height:120px; object-fit:contain; border:1px solid var(--border); border-radius:6px; background:#111827; }
.paste-item-header { display:flex; justify-content:space-between; gap:8px; align-items:center; margin-bottom:7px; font-size:13px; }
.paste-actions { display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-top:7px; }
.paste-actions button, .secondary { min-height:30px; font-size:13px; padding:5px 8px; }
.paste-actions button:last-child { grid-column:1 / -1; }
.secondary { background:#fff; color:var(--text); border-color:var(--border); }
.notice { border:1px solid var(--border); background:#fff; border-radius:8px; padding:14px; margin-bottom:12px; }
.grid { display:grid; grid-template-columns:minmax(0, 1fr); gap:12px; }
.study { display:grid; grid-template-columns:minmax(180px, 240px) minmax(0, 1fr); min-height:180px; background:var(--panel); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
.thumb { width:100%; min-height:180px; background:#101820; display:flex; align-items:center; justify-content:center; color:#aab6c4; }
.thumb img { width:100%; height:100%; object-fit:contain; display:block; }
.no-thumb { font-size:13px; }
.study-body { min-width:0; padding:13px 14px 12px; }
.line { display:flex; justify-content:space-between; gap:10px; color:var(--muted); font-size:13px; }
.modality { color:#0f766e; font-weight:700; }
h2 { margin:7px 0 10px; font-size:17px; line-height:1.25; letter-spacing:0; overflow-wrap:anywhere; }
dl { display:grid; grid-template-columns:1fr 1fr; gap:7px 12px; margin:0; }
dt { color:var(--muted); font-size:12px; }
dd { margin:2px 0 0; overflow-wrap:anywhere; }
.actions { display:flex; gap:8px; margin-top:13px; flex-wrap:wrap; }
.aio-panel { margin-top:13px; border:1px solid var(--border); border-radius:8px; background:#f8fafc; padding:10px; }
.aio-panel h3 { margin:0 0 8px; font-size:15px; line-height:1.25; letter-spacing:0; }
.aio-disclaimer { margin:0 0 9px; padding:8px; border:1px solid #f2c94c; border-radius:6px; background:#fff8db; color:#4a3412; font:700 12px/1.35 system-ui, -apple-system, Segoe UI, sans-serif; white-space:pre-wrap; }
.aio-content p { margin:0 0 8px; }
.aio-fields { display:grid; grid-template-columns:1fr; gap:6px; margin:0 0 9px; }
.aio-field { display:grid; grid-template-columns:120px minmax(0, 1fr); gap:8px; font-size:13px; }
.aio-field span { color:var(--muted); }
.aio-field strong { font-weight:600; overflow-wrap:anywhere; }
.aio-helper { border:1px solid var(--border); border-radius:8px; background:#fff; padding:9px; margin:0 0 9px; }
.aio-helper h4 { margin:0 0 7px; font-size:14px; line-height:1.25; letter-spacing:0; }
.aio-helper-list { display:grid; gap:7px; margin:0; padding:0; list-style:none; }
.aio-helper-list li { border-top:1px solid #edf1f6; padding-top:7px; }
.aio-helper-list li:first-child { border-top:0; padding-top:0; }
.aio-helper-list strong { display:block; font-size:13px; margin-bottom:2px; }
.aio-helper-list span { display:block; color:var(--muted); font-size:12px; line-height:1.35; }
.aio-score-meta { margin:0 0 7px; color:var(--muted); font-size:12px; line-height:1.35; }
.aio-score-list { display:grid; gap:5px; margin:0; padding:0; list-style:none; }
.aio-score-list li { display:grid; grid-template-columns:minmax(0, 1fr) 64px; gap:8px; align-items:center; font-size:13px; }
.aio-score-list strong { font-weight:600; overflow-wrap:anywhere; }
.aio-score-list span { text-align:right; font-variant-numeric:tabular-nums; color:#0f766e; font-weight:700; }
.aio-controls { display:flex; gap:8px; flex-wrap:wrap; }
.aio-controls button[disabled] { opacity:.55; cursor:not-allowed; }
.empty, .error { border:1px solid var(--border); background:#fff; border-radius:8px; padding:18px; }
.error { border-color:#ef9a9a; color:#9f1239; }
@media (max-width: 720px) {
  .topbar { display:block; padding:18px; }
  .search { margin-top:14px; min-width:0; }
  main { padding:14px; }
  .grid { grid-template-columns:1fr; }
  .study { grid-template-columns:108px minmax(0, 1fr); }
  .thumb { width:108px; }
  dl { grid-template-columns:1fr; }
  .patient-context { grid-template-columns:1fr 1fr; }
}
"""


AIO_PANEL_SCRIPT = r"""
(function () {
  const panels = document.querySelectorAll("[data-aio-panel]");
  if (!panels.length) return;

  function loadPanel(panel) {
    const studyInstanceUid = panel.dataset.studyInstanceUid || "";
    if (!studyInstanceUid) return;
    fetch("/api/aio/study/" + encodeURIComponent(studyInstanceUid), {
      headers: { "Accept": "application/json" }
    })
      .then(function (response) {
        if (!response.ok) throw new Error("AIO lookup failed");
        return response.json();
      })
      .then(function (payload) {
        const reports = Array.isArray(payload.reports) ? payload.reports : [];
        if (reports.length === 0) {
          renderNoReport(panel);
          return;
        }
        renderReport(panel, reports[0]);
      })
      .catch(function () {
        renderUnavailable(panel);
      });
  }

  function renderNoReport(panel) {
    const content = panel.querySelector(".aio-content");
    content.textContent = "";
    const message = document.createElement("p");
    message.textContent = "No AI Opinion yet";
    const run = document.createElement("button");
    run.type = "button";
    run.textContent = "Run AI Opinion";
    run.addEventListener("click", function () { runOpinion(panel, run); });
    content.appendChild(message);
    content.appendChild(run);
  }

  function runOpinion(panel, button) {
    const orthancStudyId = panel.dataset.orthancStudyId || "";
    if (!orthancStudyId) return;
    button.disabled = true;
    button.textContent = "Running AI Opinion";
    fetch("/api/aio/infer/" + encodeURIComponent(orthancStudyId), {
      method: "POST",
      headers: { "Accept": "application/json" }
    })
      .then(function (response) {
        if (!response.ok) throw new Error("AIO infer failed");
        return response.json();
      })
      .then(function (report) {
        renderReport(panel, report);
      })
      .catch(function () {
        renderUnavailable(panel);
      });
  }

  function renderReport(panel, report) {
    const disclaimer = panel.querySelector(".aio-disclaimer");
    if (disclaimer && report.disclaimer_text) {
      disclaimer.textContent = report.disclaimer_text;
    }

    const content = panel.querySelector(".aio-content");
    content.textContent = "";
    const fields = document.createElement("div");
    fields.className = "aio-fields";
    fields.appendChild(field("status", report.status));
    fields.appendChild(field("ai_domain", report.ai_domain));
    fields.appendChild(field("model_name", report.model_name || "-"));
    fields.appendChild(field("model_version", report.model_version || "-"));
    fields.appendChild(field("summary", report.summary || "-"));
    fields.appendChild(field("routing reason", routingReason(report)));
    fields.appendChild(field("physician_review_status", report.physician_review_status || "-"));
    fields.appendChild(field("disclaimer_text", report.disclaimer_text || "-"));

    const controls = document.createElement("div");
    controls.className = "aio-controls";
    const reviewed = document.createElement("button");
    reviewed.type = "button";
    reviewed.textContent = "Mark reviewed";
    reviewed.disabled = report.physician_review_status === "approved";
    reviewed.addEventListener("click", function () {
      reviewed.disabled = true;
      fetch("/api/aio/report/" + encodeURIComponent(report.id) + "/review", {
        method: "POST",
        headers: { "Accept": "application/json" }
      })
        .then(function (response) {
          if (!response.ok) throw new Error("AIO review failed");
          return response.json();
        })
        .then(function (updated) { renderReport(panel, updated); })
        .catch(function () {
          reviewed.disabled = false;
        });
    });

    const reject = document.createElement("button");
    reject.type = "button";
    reject.textContent = "Reject/Hide";
    reject.disabled = true;
    reject.title = "TODO: enable when the AIO API supports hide/reject workflow semantics.";

    controls.appendChild(reviewed);
    controls.appendChild(reject);
    content.appendChild(fields);
    const helper = helperPanel(report);
    if (helper) content.appendChild(helper);
    const scores = scorePanel(report);
    if (scores) content.appendChild(scores);
    content.appendChild(controls);
  }

  function helperPanel(report) {
    const findings = Array.isArray(report.findings_json) ? report.findings_json : [];
    const helperFindings = findings.filter(function (item) {
      return item && item.section !== "torchxrayvision_scores";
    });
    if (!helperFindings.length) return null;

    const panel = document.createElement("div");
    panel.className = "aio-helper";
    const title = document.createElement("h4");
    title.textContent = report.ai_domain === "cxr" ? "Chest X-ray helper" : "AI Opinion helper";
    const list = document.createElement("ul");
    list.className = "aio-helper-list";

    helperFindings.forEach(function (item) {
      const row = document.createElement("li");
      const label = document.createElement("strong");
      label.textContent = item.label || item.section || "Review item";
      const prompt = document.createElement("span");
      const status = item.status ? " [" + item.status + "]" : "";
      prompt.textContent = (item.helper_prompt || "-") + status;
      row.appendChild(label);
      row.appendChild(prompt);
      list.appendChild(row);
    });

    panel.appendChild(title);
    panel.appendChild(list);
    return panel;
  }

  function scorePanel(report) {
    const findings = Array.isArray(report.findings_json) ? report.findings_json : [];
    const item = findings.find(function (entry) {
      return entry && entry.section === "torchxrayvision_scores" && entry.scores && typeof entry.scores === "object";
    });
    if (!item) return null;

    const entries = Object.entries(item.scores)
      .filter(function (entry) { return Number.isFinite(Number(entry[1])); })
      .sort(function (a, b) { return Number(b[1]) - Number(a[1]); })
      .slice(0, 8);
    if (!entries.length) return null;

    const panel = document.createElement("div");
    panel.className = "aio-helper";
    const title = document.createElement("h4");
    title.textContent = "TorchXRayVision testing scores";

    const meta = document.createElement("p");
    meta.className = "aio-score-meta";
    meta.textContent = [
      item.score_type || "model output",
      item.input_source || "",
      item.input_warning || ""
    ].filter(Boolean).join(" · ");

    const list = document.createElement("ul");
    list.className = "aio-score-list";
    entries.forEach(function (entry) {
      const row = document.createElement("li");
      const label = document.createElement("strong");
      label.textContent = entry[0];
      const value = document.createElement("span");
      value.textContent = Number(entry[1]).toFixed(3);
      row.appendChild(label);
      row.appendChild(value);
      list.appendChild(row);
    });

    panel.appendChild(title);
    panel.appendChild(meta);
    panel.appendChild(list);
    return panel;
  }

  function renderUnavailable(panel) {
    const content = panel.querySelector(".aio-content");
    content.textContent = "";
    const message = document.createElement("p");
    message.textContent = "AI Opinion service is unavailable.";
    content.appendChild(message);
  }

  function field(label, value) {
    const row = document.createElement("div");
    row.className = "aio-field";
    const key = document.createElement("span");
    key.textContent = label;
    const val = document.createElement("strong");
    val.textContent = value == null || value === "" ? "-" : String(value);
    row.appendChild(key);
    row.appendChild(val);
    return row;
  }

  function routingReason(report) {
    if (!report || !report.routing_json) return "-";
    return report.routing_json.reason || "-";
  }

  panels.forEach(loadPanel);
})();
"""


PASTE_SCRIPT = r"""
(function () {
  const form = document.querySelector("[data-paste-upload]");
  if (!form) return;
  const input = form.querySelector("#file");
  const zone = form.querySelector("#paste-zone");
  const queue = form.querySelector("#paste-queue");
  const clear = form.querySelector("#paste-clear");
  const status = form.querySelector("#paste-status");
  if (!input || !zone || !queue || !clear || !status) return;
  const pastedFiles = [];
  const objectUrls = new Map();
  let syncingInput = false;

  function setStatus(message) {
    status.textContent = message;
  }

  function syncInputFiles() {
    if (!window.DataTransfer) {
      setStatus("This browser cannot attach pasted images. Use the file picker instead.");
      return;
    }
    const transfer = new DataTransfer();
    pastedFiles.forEach(function (file) { transfer.items.add(file); });
    syncingInput = true;
    input.files = transfer.files;
    syncingInput = false;
  }

  function revokeUrl(file) {
    const url = objectUrls.get(file);
    if (url) {
      URL.revokeObjectURL(url);
      objectUrls.delete(file);
    }
  }

  function renderQueue() {
    queue.textContent = "";
    pastedFiles.forEach(function (file, index) {
      let url = objectUrls.get(file);
      if (!url) {
        url = URL.createObjectURL(file);
        objectUrls.set(file, url);
      }

      const item = document.createElement("div");
      item.className = "paste-item";

      const header = document.createElement("div");
      header.className = "paste-item-header";
      const title = document.createElement("strong");
      title.textContent = "Item " + (index + 1);
      const size = document.createElement("span");
      size.textContent = Math.ceil(file.size / 1024) + " KB";
      header.appendChild(title);
      header.appendChild(size);

      const image = document.createElement("img");
      image.alt = "Queued pasted image " + (index + 1);
      image.src = url;

      const actions = document.createElement("div");
      actions.className = "paste-actions";
      const up = actionButton("Move up", function () { moveItem(index, -1); });
      const down = actionButton("Move down", function () { moveItem(index, 1); });
      const remove = actionButton("Remove", function () { removeItem(index); });
      up.disabled = index === 0;
      down.disabled = index === pastedFiles.length - 1;
      actions.appendChild(up);
      actions.appendChild(down);
      actions.appendChild(remove);

      item.appendChild(header);
      item.appendChild(image);
      item.appendChild(actions);
      queue.appendChild(item);
    });
    clear.disabled = pastedFiles.length === 0;
    if (pastedFiles.length > 0) {
      setStatus(pastedFiles.length + " pasted image" + (pastedFiles.length === 1 ? "" : "s") + " queued. Press Upload to add them to PACS.");
    }
  }

  function actionButton(label, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = label;
    button.addEventListener("click", onClick);
    return button;
  }

  function moveItem(index, direction) {
    const next = index + direction;
    if (next < 0 || next >= pastedFiles.length) return;
    const current = pastedFiles[index];
    pastedFiles[index] = pastedFiles[next];
    pastedFiles[next] = current;
    syncInputFiles();
    renderQueue();
  }

  function removeItem(index) {
    const removed = pastedFiles.splice(index, 1)[0];
    if (removed) revokeUrl(removed);
    syncInputFiles();
    renderQueue();
    if (pastedFiles.length === 0) setStatus("Paste images, or use the file picker.");
  }

  function clearQueue() {
    pastedFiles.splice(0).forEach(revokeUrl);
    if (window.DataTransfer) input.files = new DataTransfer().files;
    renderQueue();
    setStatus("Pasted image queue cleared.");
  }

  function addPastedFile(file) {
    const name = "pasted-image-" + String(pastedFiles.length + 1).padStart(2, "0") + ".png";
    const pasted = new File([file], name, { type: file.type || "image/png" });
    pastedFiles.push(pasted);
    syncInputFiles();
    renderQueue();
  }

  function handlePaste(event) {
    const clipboard = event.clipboardData;
    if (!clipboard || !clipboard.items) return;
    for (const item of clipboard.items) {
      if (item.kind === "file" && item.type && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) {
          event.preventDefault();
          addPastedFile(file);
          return;
        }
      }
    }
    setStatus("Clipboard does not contain an image.");
  }

  zone.addEventListener("click", function () { zone.focus(); });
  zone.addEventListener("paste", handlePaste);
  clear.addEventListener("click", clearQueue);
  input.addEventListener("change", function () {
    if (syncingInput || pastedFiles.length === 0) return;
    pastedFiles.splice(0).forEach(revokeUrl);
    queue.textContent = "";
    clear.disabled = true;
    setStatus("File picker selection will be uploaded.");
  });
  document.addEventListener("paste", function (event) {
    if (document.activeElement === zone) return;
    if (form.contains(document.activeElement) || document.activeElement === document.body) {
      handlePaste(event);
    }
  });
  clear.disabled = true;
})();
"""


def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config()
    orthanc = OrthancClient(config.orthanc_url)
    LOGGER.info(
        "Starting KaosPACS Web host=%s port=%s orthanc_url=%s weasis_dicomweb_url=%s",
        config.http_host,
        config.http_port,
        config.orthanc_url,
        config.weasis_dicomweb_url,
    )
    server = ThreadingHTTPServer(
        (config.http_host, config.http_port),
        create_handler(config, orthanc),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
