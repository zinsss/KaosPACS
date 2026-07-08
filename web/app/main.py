from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, parse, request

from flask import Flask, redirect, render_template_string, request as flask_request, url_for


TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>KaosPACS Imaging Worklist</title>
    <style>
      body { font-family: sans-serif; margin: 24px; }
      table { border-collapse: collapse; width: 100%; }
      th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
      .filters a { margin-right: 10px; }
      .message { margin: 12px 0; color: #0a7; }
      .error { margin: 12px 0; color: #b00; }
      .mark-complete { background: #f9e2af; border: 1px solid #d9b96c; padding: 6px 10px; }
    </style>
  </head>
  <body>
    <h1>KaosPACS Imaging Worklist</h1>
    {% if message %}<div class="message">{{ message }}</div>{% endif %}
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <div class="filters">
      {% for option in ["active", "completed", "expired", "cancelled", "all"] %}
        <a href="{{ url_for('imaging_worklist', filter=option) }}">{{ option.title() }}</a>
      {% endfor %}
    </div>
    <table>
      <thead>
        <tr>
          <th>State</th>
          <th>Accession</th>
          <th>Patient ID</th>
          <th>Patient Name</th>
          <th>DOB</th>
          <th>Sex</th>
          <th>Modality</th>
          <th>Scheduled At</th>
          <th>Description</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        {% for entry in entries %}
          <tr>
            <td>{{ entry["state"] }}</td>
            <td>{{ entry["AccessionNumber"] }}</td>
            <td>{{ entry["PatientID"] }}</td>
            <td>{{ entry["PatientName"] }}</td>
            <td>{{ entry["PatientBirthDate"] }}</td>
            <td>{{ entry["PatientSex"] }}</td>
            <td>{{ entry["Modality"] }}</td>
            <td>{{ entry["ScheduledAt"] }}</td>
            <td>{{ entry["Description"] }}</td>
            <td>
              {% if entry["state"] in ["active", "inactive"] %}
              <form method="post" action="{{ url_for('mark_complete') }}" onsubmit="return confirm('Mark this study complete?');">
                <input type="hidden" name="AccessionNumber" value="{{ entry['AccessionNumber'] }}">
                <select name="CompleteReason">
                  <option value="orthanc_study_present_manual_correction">orthanc_study_present_manual_correction</option>
                  <option value="accession_mismatch_corrected">accession_mismatch_corrected</option>
                  <option value="gateway_match_missed">gateway_match_missed</option>
                  <option value="operator_verified_completed">operator_verified_completed</option>
                  <option value="other">other</option>
                </select>
                <button class="mark-complete" type="submit">Mark Complete</button>
              </form>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </body>
</html>
"""


def _request_json(
    *,
    base_url: str,
    path: str,
    method: str,
    payload: dict[str, Any] | None = None,
    token: str = "",
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or exc.reason) from exc
    except error.URLError as exc:
        raise RuntimeError("gateway unavailable") from exc
    return json.loads(body) if body else {}


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        GATEWAY_BASE_URL=os.getenv("GATEWAY_BASE_URL", "http://127.0.0.1:8060"),
        GATEWAY_API_TOKEN=os.getenv("GATEWAY_API_TOKEN", ""),
        GATEWAY_TIMEOUT_SECONDS=float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "5")),
        FETCH_IMAGING_WORKLIST=None,
        MARK_COMPLETE=None,
    )
    if test_config:
        app.config.update(test_config)

    def fetch_imaging_worklist() -> list[dict[str, Any]]:
        custom = app.config.get("FETCH_IMAGING_WORKLIST")
        if callable(custom):
            return custom()
        payload = _request_json(
            base_url=app.config["GATEWAY_BASE_URL"],
            path="/imaging/worklist",
            method="GET",
            token=app.config["GATEWAY_API_TOKEN"],
            timeout_seconds=app.config["GATEWAY_TIMEOUT_SECONDS"],
        )
        entries = payload.get("entries", [])
        return [entry for entry in entries if isinstance(entry, dict)]

    def mark_complete_request(payload: dict[str, Any]) -> dict[str, Any]:
        custom = app.config.get("MARK_COMPLETE")
        if callable(custom):
            return custom(payload)
        return _request_json(
            base_url=app.config["GATEWAY_BASE_URL"],
            path="/admin/worklist/complete",
            method="POST",
            payload=payload,
            token=app.config["GATEWAY_API_TOKEN"],
            timeout_seconds=app.config["GATEWAY_TIMEOUT_SECONDS"],
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/imaging/worklist")
    def imaging_worklist():
        selected_filter = (flask_request.args.get("filter") or "active").strip().lower()
        message = flask_request.args.get("message", "")
        error_message = flask_request.args.get("error", "")
        try:
            entries = fetch_imaging_worklist()
        except RuntimeError:
            entries = []
            error_message = "KaosPACS Gateway unavailable"
        if selected_filter != "all":
            entries = [entry for entry in entries if entry.get("state") == selected_filter]
        return render_template_string(
            TEMPLATE,
            entries=entries,
            message=message,
            error=error_message,
        )

    @app.post("/imaging/worklist/mark-complete")
    def mark_complete():
        accession_number = (flask_request.form.get("AccessionNumber") or "").strip()
        reason = (flask_request.form.get("CompleteReason") or "").strip()
        payload = {
            "AccessionNumber": accession_number,
            "CompleteReason": reason,
        }
        try:
            mark_complete_request(payload)
        except RuntimeError:
            query = parse.urlencode({"filter": "all", "error": "Mark Complete failed"})
            return redirect(f"{url_for('imaging_worklist')}?{query}")
        query = parse.urlencode({"filter": "completed", "message": "Mark Complete requested"})
        return redirect(f"{url_for('imaging_worklist')}?{query}")

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "8070"))
    app.run(host=host, port=port)
