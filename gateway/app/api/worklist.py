from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from app.api import (
    audit_event,
    gateway_bad_gateway,
    json_response,
    mwl_client,
    proxy_mwl_response,
    read_json_request,
    text,
)
from app.clients.mwl import MwlHttpError, MwlUnavailableError


def validate_worklist_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    if not isinstance(payload.get("entries"), list):
        return ["payload.entries must be a list"]
    return []


def validate_state_request(payload: Any, allow_cancel_reason: bool) -> list[str]:
    if not isinstance(payload, dict):
        return ["body must be an object"]
    if not text(payload.get("AccessionNumber")):
        return ["AccessionNumber is required"]
    if not allow_cancel_reason and "CancelReason" in payload:
        return ["CancelReason is only valid for cancel requests"]
    return []


def workflow_event_type(path: str) -> str:
    if path == "/worklist":
        return "worklist_get"
    if path == "/worklist/cancel":
        return "worklist_cancel"
    if path == "/worklist/complete":
        return "worklist_complete"
    return "worklist_put"


def handle_get_worklist(handler: BaseHTTPRequestHandler, path: str) -> None:
    try:
        response = mwl_client(handler).get_worklist()
    except MwlHttpError as error:
        audit_event(
            handler,
            event_type="worklist_get",
            request_path=path,
            status_code=error.status_code,
            success=False,
            error_code="mwl_error",
        )
        proxy_mwl_response(handler, error.status_code, error.payload)
        return
    except MwlUnavailableError:
        audit_event(
            handler,
            event_type="mwl_unavailable",
            request_path=path,
            status_code=HTTPStatus.BAD_GATEWAY,
            success=False,
            error_code="mwl_unavailable",
        )
        gateway_bad_gateway(handler)
        return

    audit_event(
        handler,
        event_type="worklist_get",
        request_path=path,
        status_code=response.status_code,
        success=response.status_code < 400,
    )
    proxy_mwl_response(handler, response.status_code, response.payload)


def handle_put_worklist(handler: BaseHTTPRequestHandler, path: str) -> None:
    try:
        payload = read_json_request(handler)
    except json.JSONDecodeError as error:
        audit_event(
            handler,
            event_type="validation_error",
            request_path=path,
            status_code=HTTPStatus.BAD_REQUEST,
            success=False,
            error_code="invalid_json",
        )
        json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid JSON", "details": [str(error)]},
        )
        return

    errors = validate_worklist_payload(payload)
    if errors:
        audit_event(
            handler,
            event_type="validation_error",
            request_path=path,
            status_code=HTTPStatus.BAD_REQUEST,
            success=False,
            error_code="invalid_worklist",
        )
        json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid worklist", "details": errors},
        )
        return

    try:
        response = mwl_client(handler).put_worklist(payload)
    except MwlHttpError as error:
        audit_event(
            handler,
            event_type="worklist_put",
            request_path=path,
            status_code=error.status_code,
            success=False,
            error_code="mwl_error",
        )
        proxy_mwl_response(handler, error.status_code, error.payload)
        return
    except MwlUnavailableError:
        audit_event(
            handler,
            event_type="mwl_unavailable",
            request_path=path,
            status_code=HTTPStatus.BAD_GATEWAY,
            success=False,
            error_code="mwl_unavailable",
        )
        gateway_bad_gateway(handler)
        return

    audit_event(
        handler,
        event_type="worklist_put",
        request_path=path,
        status_code=response.status_code,
        success=response.status_code < 400,
    )
    proxy_mwl_response(handler, response.status_code, response.payload)


def handle_worklist_state_post(handler: BaseHTTPRequestHandler, path: str) -> None:
    try:
        payload = read_json_request(handler)
    except json.JSONDecodeError as error:
        audit_event(
            handler,
            event_type="validation_error",
            request_path=path,
            status_code=HTTPStatus.BAD_REQUEST,
            success=False,
            error_code="invalid_json",
        )
        json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid JSON", "details": [str(error)]},
        )
        return

    allow_cancel_reason = path == "/worklist/cancel"
    errors = validate_state_request(payload, allow_cancel_reason)
    accession_number = text(payload.get("AccessionNumber")) if isinstance(payload, dict) else ""
    if errors:
        audit_event(
            handler,
            event_type="validation_error",
            request_path=path,
            accession_number=accession_number or None,
            status_code=HTTPStatus.BAD_REQUEST,
            success=False,
            error_code="invalid_request",
        )
        json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid request", "details": errors},
        )
        return

    try:
        if path == "/worklist/complete":
            response = mwl_client(handler).complete_worklist(payload)
        else:
            response = mwl_client(handler).cancel_worklist(payload)
    except MwlHttpError as error:
        audit_event(
            handler,
            event_type=workflow_event_type(path),
            request_path=path,
            accession_number=accession_number,
            status_code=error.status_code,
            success=False,
            error_code="mwl_error",
        )
        proxy_mwl_response(handler, error.status_code, error.payload)
        return
    except MwlUnavailableError:
        audit_event(
            handler,
            event_type="mwl_unavailable",
            request_path=path,
            accession_number=accession_number,
            status_code=HTTPStatus.BAD_GATEWAY,
            success=False,
            error_code="mwl_unavailable",
        )
        gateway_bad_gateway(handler)
        return

    audit_event(
        handler,
        event_type=workflow_event_type(path),
        request_path=path,
        accession_number=accession_number,
        status_code=response.status_code,
        success=response.status_code < 400,
    )
    proxy_mwl_response(handler, response.status_code, response.payload)
