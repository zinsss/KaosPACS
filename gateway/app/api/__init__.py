from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from app.clients.mwl import MwlApiClient
from app.services.audit import record_gateway_event


def json_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_request(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        raw = b"{}"
    return json.loads(raw.decode("utf-8"))


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def mwl_client(handler: BaseHTTPRequestHandler) -> MwlApiClient:
    return MwlApiClient(
        handler.config.mwl_api_url,
        handler.config.mwl_api_timeout_seconds,
    )


def audit_event(
    handler: BaseHTTPRequestHandler,
    *,
    event_type: str,
    request_path: str,
    status_code: int | None,
    success: bool,
    error_code: str | None = None,
    accession_number: str | None = None,
) -> None:
    record_gateway_event(
        handler.config.gateway_audit_db,
        event_type=event_type,
        request_path=request_path,
        accession_number=accession_number,
        status_code=status_code,
        success=success,
        error_code=error_code,
    )


def gateway_bad_gateway(handler: BaseHTTPRequestHandler) -> None:
    json_response(
        handler,
        HTTPStatus.BAD_GATEWAY,
        {
            "error": "bad_gateway",
            "message": "MWL API is unavailable",
        },
    )


def proxy_mwl_response(handler: BaseHTTPRequestHandler, status_code: int, payload: Any) -> None:
    try:
        status = HTTPStatus(status_code)
    except ValueError:
        status = HTTPStatus.BAD_GATEWAY
    json_response(handler, status, payload)
