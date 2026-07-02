from __future__ import annotations

import html
import json
import logging
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from .config import Config, load_config
from .orthanc import OrthancClient, StudySummary


LOGGER = logging.getLogger("kaospacs.web")


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
            if parsed.path == "/api/studies":
                self._api_studies(parsed.query)
                return
            if parsed.path.startswith("/thumbnail/"):
                self._thumbnail(parsed.path.removeprefix("/thumbnail/"))
                return
            if parsed.path in ("/", "/emr.php"):
                self._index(parsed.query)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.info("Web %s", fmt % args)

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

        def _index(self, query_string: str) -> None:
            params = parse_qs(query_string)
            query = params.get("q", [""])[0]
            try:
                studies = orthanc.studies(query=query, limit=config.study_limit)
                body = render_index(config, studies, query=query, error="")
            except Exception as exc:
                LOGGER.warning("Orthanc page query failed exception=%s", exc.__class__.__name__)
                body = render_index(config, [], query=query, error="Orthanc is not reachable.")
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

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
    error: str,
) -> str:
    rows = "\n".join(_study_card(config, study) for study in studies)
    if not rows and not error:
        rows = '<div class="empty">No studies found in Orthanc.</div>'
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
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
}
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
