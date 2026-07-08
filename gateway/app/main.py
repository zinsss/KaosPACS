from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any
from urllib import error, request

from flask import Flask, jsonify, request as flask_request


LOGGER = logging.getLogger("kaospacs.gateway")
ALLOWED_COMPLETE_REASONS = {
    "orthanc_study_present_manual_correction",
    "accession_mismatch_corrected",
    "gateway_match_missed",
    "operator_verified_completed",
    "other",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    raw = value
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _state_for_entry(entry: dict[str, Any], now: datetime | None = None) -> str:
    if _text(entry.get("CancelledAt")):
        return "cancelled"
    if _text(entry.get("CompletedAt")):
        return "completed"
    active = entry.get("Active", True)
    if active is False:
        return "inactive"
    expires_at = _parse_iso(_text(entry.get("ExpiresAt")))
    if expires_at is not None:
        effective_now = now or datetime.now().astimezone()
        if expires_at <= effective_now:
            return "expired"
    if _text(entry.get("ExpiredAt")):
        return "expired"
    return "active"


def _scheduled_at(entry: dict[str, Any]) -> str:
    date = _text(entry.get("ScheduledProcedureStepStartDate"))
    time = _text(entry.get("ScheduledProcedureStepStartTime"))
    if not date:
        return ""
    if len(date) == 8:
        formatted_date = f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
    else:
        formatted_date = date
    if not time:
        return formatted_date
    if len(time) >= 6:
        formatted_time = f"{time[0:2]}:{time[2:4]}:{time[4:6]}"
    else:
        formatted_time = time
    return f"{formatted_date}T{formatted_time}"


def _description(entry: dict[str, Any]) -> str:
    return (
        _text(entry.get("RequestedProcedureDescription"))
        or _text(entry.get("StudyDescription"))
        or _text(entry.get("ScheduledProcedureStepDescription"))
    )


def _map_imaging_entry(entry: dict[str, Any]) -> dict[str, Any]:
    state = _state_for_entry(entry)
    expired_at = _text(entry.get("ExpiredAt"))
    if not expired_at and state == "expired":
        expired_at = _text(entry.get("ExpiresAt"))
    return {
        "state": state,
        "AccessionNumber": _text(entry.get("AccessionNumber")),
        "PatientID": _text(entry.get("PatientID")),
        "PatientName": _text(entry.get("PatientName")),
        "PatientBirthDate": _text(entry.get("PatientBirthDate")),
        "PatientSex": _text(entry.get("PatientSex")),
        "Modality": _text(entry.get("Modality")),
        "ScheduledStationAETitle": _text(entry.get("ScheduledStationAETitle")),
        "ScheduledAt": _scheduled_at(entry),
        "Description": _description(entry),
        "CompletedAt": _text(entry.get("CompletedAt")),
        "CompleteReason": _text(entry.get("CompleteReason")),
        "ExpiredAt": expired_at,
        "ExpireReason": _text(entry.get("ExpireReason")),
        "CancelledAt": _text(entry.get("CancelledAt")),
        "CancelReason": _text(entry.get("CancelReason")),
    }


def _request_json(
    *,
    base_url: str,
    path: str,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json; charset=utf-8"}
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
        raise RuntimeError(f"http {exc.code}: {detail or exc.reason}") from exc
    except error.URLError as exc:
        raise RuntimeError("mwl unavailable") from exc
    return json.loads(body) if body else {}


def _require_bearer(app: Flask):
    expected = (app.config.get("GATEWAY_API_TOKEN") or "").strip()
    if not expected:
        return jsonify({"error": "admin token not configured"}), 503

    header = flask_request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401
    token = header[len("Bearer ") :].strip()
    if token != expected:
        return jsonify({"error": "unauthorized"}), 401
    return None


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        MWL_API_BASE_URL=os.getenv("MWL_API_BASE_URL", "http://127.0.0.1:8055"),
        GATEWAY_TIMEOUT_SECONDS=float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "5")),
        GATEWAY_API_TOKEN=os.getenv("GATEWAY_API_TOKEN", ""),
        FETCH_WORKLIST=None,
        COMPLETE_WORKLIST_ENTRY=None,
    )
    if test_config:
        app.config.update(test_config)

    def fetch_worklist() -> list[dict[str, Any]]:
        custom = app.config.get("FETCH_WORKLIST")
        if callable(custom):
            return custom()
        payload = _request_json(
            base_url=app.config["MWL_API_BASE_URL"],
            path="/worklist",
            method="GET",
            timeout_seconds=app.config["GATEWAY_TIMEOUT_SECONDS"],
        )
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise RuntimeError("invalid mwl payload")
        return [entry for entry in entries if isinstance(entry, dict)]

    def complete_entry(payload: dict[str, Any]) -> dict[str, Any]:
        custom = app.config.get("COMPLETE_WORKLIST_ENTRY")
        if callable(custom):
            return custom(payload)
        return _request_json(
            base_url=app.config["MWL_API_BASE_URL"],
            path="/worklist/complete",
            method="POST",
            payload=payload,
            timeout_seconds=app.config["GATEWAY_TIMEOUT_SECONDS"],
        )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/imaging/worklist")
    def imaging_worklist():
        try:
            entries = fetch_worklist()
        except RuntimeError:
            return jsonify({"error": "gateway unavailable"}), 502
        return jsonify({"entries": [_map_imaging_entry(entry) for entry in entries]})

    @app.post("/admin/worklist/complete")
    def admin_complete():
        auth_error = _require_bearer(app)
        if auth_error is not None:
            return auth_error

        payload = flask_request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "invalid request"}), 400

        accession_number = _text(payload.get("AccessionNumber"))
        reason = _text(payload.get("CompleteReason"))
        if not accession_number:
            return jsonify({"error": "AccessionNumber is required"}), 400
        if not reason:
            return jsonify({"error": "CompleteReason is required"}), 400
        if reason not in ALLOWED_COMPLETE_REASONS:
            return jsonify({"error": "unsupported CompleteReason"}), 400

        try:
            entries = fetch_worklist()
        except RuntimeError as exc:
            LOGGER.warning(
                "event=admin_complete result=unavailable reason=%s error=%s",
                reason,
                exc.__class__.__name__,
            )
            return jsonify({"error": "gateway unavailable"}), 502

        entry = next(
            (
                candidate
                for candidate in entries
                if _text(candidate.get("AccessionNumber")) == accession_number
            ),
            None,
        )
        if entry is None:
            return jsonify({"error": "not found"}), 404

        current_state = _state_for_entry(entry)
        if current_state not in {"active", "inactive"}:
            return (
                jsonify(
                    {
                        "error": "state not eligible",
                        "state": current_state,
                    }
                ),
                409,
            )

        request_payload = {
            "AccessionNumber": accession_number,
            "CompleteReason": reason,
        }
        orthanc_uid = _text(payload.get("OrthancStudyInstanceUID"))
        note = _text(payload.get("Note"))
        if orthanc_uid:
            request_payload["OrthancStudyInstanceUID"] = orthanc_uid
        if note:
            request_payload["Note"] = note
        if payload.get("Force") is True:
            request_payload["Force"] = True

        try:
            complete_entry(request_payload)
        except RuntimeError as exc:
            LOGGER.warning(
                "event=admin_complete result=failed reason=%s error=%s",
                reason,
                exc.__class__.__name__,
            )
            return jsonify({"error": "completion failed"}), 502

        LOGGER.info("event=admin_complete result=success reason=%s", reason)
        return jsonify(
            {
                "updated": 1,
                "AccessionNumber": accession_number,
                "state": "completed",
                "completed_at": _iso_now(),
            }
        )

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    host = os.getenv("GATEWAY_HOST", "0.0.0.0")
    port = int(os.getenv("GATEWAY_PORT", "8060"))
    app.run(host=host, port=port)
