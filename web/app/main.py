from __future__ import annotations

import base64
import binascii
import hmac
import html
import json
import logging
import warnings
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

from .config import Config, load_config
from .dicom_upload import create_upload_dicom
from .orthanc import OrthancClient, StudySummary

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import cgi


LOGGER = logging.getLogger("kaospacs.web")


@dataclass(frozen=True)
class PatientContext:
    patient_id: str = ""
    patient_name: str = ""
    patient_birth_date: str = ""
    patient_sex: str = ""


def make_weasis_url(dicomweb_url: str, study_instance_uid: str) -> str:
    command = (
        f'$dicom:rs --url "{dicomweb_url.rstrip("/")}" '
        f'-r "studyUID={study_instance_uid}"'
    )
    return "weasis://?" + quote_plus(command)


def create_handler(config: Config, orthanc: OrthancClient) -> type[BaseHTTPRequestHandler]:
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
                studies = orthanc.studies(query=query, limit=limit)
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
            upload_message = _upload_status_message(upload_status)
            try:
                if path == "/emr.php" and patient.patient_id:
                    studies = orthanc.studies_for_patient(
                        patient.patient_id,
                        query=query,
                        limit=config.study_limit,
                    )
                elif path == "/emr.php":
                    studies = []
                else:
                    studies = orthanc.studies(query=query, limit=config.study_limit)
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
                if field is None or not getattr(field, "filename", ""):
                    self._redirect_upload(params, "missing_file")
                    return
                content = field.file.read(config.upload_max_bytes + 1)
                if not content:
                    self._redirect_upload(params, "missing_file")
                    return
                if len(content) > config.upload_max_bytes:
                    self._redirect_upload(params, "too_large")
                    return
                result = create_upload_dicom(
                    patient_id=patient.patient_id,
                    patient_name=patient.patient_name,
                    patient_birth_date=patient.patient_birth_date,
                    patient_sex=patient.patient_sex,
                    filename=field.filename,
                    content_type=getattr(field, "type", "") or "",
                    content=content,
                )
                orthanc.upload_instance(result.dicom_bytes)
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
                "Web upload stored event=upload_stored accession_number=%s modality=%s",
                result.accession_number,
                result.modality,
            )
            self._redirect_upload(params, "success")

        def _redirect_upload(self, params: dict[str, list[str]], status: str) -> None:
            query = {key: values[0] for key, values in params.items() if values}
            query["upload"] = status
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
    modality = ", ".join(study.modalities) or "-"
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
  </div>
</article>"""


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
      <span>Copy an image or screenshot, click here, then press Ctrl+V. Nothing needs to be saved on the desktop.</span>
      <div id="paste-preview" class="paste-preview" aria-live="polite"></div>
      <div id="paste-status" class="paste-status" aria-live="polite"></div>
    </div>
    <div class="upload-row">
      <input id="file" name="file" type="file" accept="image/jpeg,image/png,application/pdf" required>
      <button type="submit">Upload</button>
    </div>
    <p>Paste images, or choose JPG, PNG, or PDF. Uploads are stored as DICOM in Orthanc for PatientID {html.escape(patient.patient_id)}.</p>
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


def _upload_status_message(status: str) -> str:
    return {
        "success": "Upload added to PACS.",
        "missing_patient": "No patient/chart number was provided.",
        "missing_file": "Choose a JPG, PNG, or PDF file to upload.",
        "too_large": "The selected file is too large.",
        "unsupported_upload_type": "Only JPG, PNG, and PDF files are supported.",
        "invalid_pdf": "The uploaded PDF file is not valid.",
        "failed": "Upload failed while saving to PACS.",
    }.get(status, "")


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
.paste-preview { margin-top:10px; }
.paste-preview img { display:block; max-width:220px; max-height:160px; object-fit:contain; border:1px solid var(--border); border-radius:6px; background:#111827; }
.notice { border:1px solid var(--border); background:#fff; border-radius:8px; padding:14px; margin-bottom:12px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(360px, 1fr)); gap:12px; }
.study { display:grid; grid-template-columns:132px minmax(0, 1fr); min-height:172px; background:var(--panel); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
.thumb { width:132px; min-height:172px; background:#101820; display:flex; align-items:center; justify-content:center; color:#aab6c4; }
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


PASTE_SCRIPT = r"""
(function () {
  const form = document.querySelector("[data-paste-upload]");
  if (!form) return;
  const input = form.querySelector("#file");
  const zone = form.querySelector("#paste-zone");
  const preview = form.querySelector("#paste-preview");
  const status = form.querySelector("#paste-status");
  if (!input || !zone || !preview || !status) return;

  function setStatus(message) {
    status.textContent = message;
  }

  function setPastedFile(file) {
    if (!window.DataTransfer) {
      setStatus("This browser cannot attach pasted images. Use the file picker instead.");
      return;
    }
    const name = file.name || "pasted-image.png";
    const pasted = new File([file], name, { type: file.type || "image/png" });
    const transfer = new DataTransfer();
    transfer.items.add(pasted);
    input.files = transfer.files;

    preview.textContent = "";
    const image = document.createElement("img");
    image.alt = "Pasted image preview";
    image.src = URL.createObjectURL(pasted);
    image.onload = function () { URL.revokeObjectURL(image.src); };
    preview.appendChild(image);
    setStatus("Pasted image is ready. Press Upload to add it to PACS.");
  }

  function handlePaste(event) {
    const clipboard = event.clipboardData;
    if (!clipboard || !clipboard.items) return;
    for (const item of clipboard.items) {
      if (item.kind === "file" && item.type && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) {
          event.preventDefault();
          setPastedFile(file);
          return;
        }
      }
    }
    setStatus("Clipboard does not contain an image.");
  }

  zone.addEventListener("click", function () { zone.focus(); });
  zone.addEventListener("paste", handlePaste);
  document.addEventListener("paste", function (event) {
    if (document.activeElement === zone) return;
    if (form.contains(document.activeElement) || document.activeElement === document.body) {
      handlePaste(event);
    }
  });
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
