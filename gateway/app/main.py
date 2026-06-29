from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .audit import init_audit_db, record_gateway_event
from .auth import is_auth_enabled, is_authorized
from .config import GatewayConfig, load_config
from .health import health_payload
from .mwl_client import MwlApiClient, MwlHttpError, MwlUnavailableError
from .orders import (
    order_to_mwl_entry,
    text as order_text,
    upsert_worklist_entry,
    validate_order_cancel,
    validate_order_upsert,
)
from .status import status_payload


LOGGER = logging.getLogger("kaospacs.gateway")
PROTECTED_ENDPOINTS = {
    ("GET", "/status"),
    ("GET", "/worklist"),
    ("PUT", "/worklist"),
    ("POST", "/worklist/complete"),
    ("POST", "/worklist/cancel"),
    ("POST", "/orders/upsert"),
    ("POST", "/orders/cancel"),
}


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _json_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_request(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        raw = b"{}"
    return json.loads(raw.decode("utf-8"))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _validate_worklist_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    if not isinstance(payload.get("entries"), list):
        return ["payload.entries must be a list"]
    return []


def _validate_state_request(payload: Any, allow_cancel_reason: bool) -> list[str]:
    if not isinstance(payload, dict):
        return ["body must be an object"]
    if not _text(payload.get("AccessionNumber")):
        return ["AccessionNumber is required"]
    if not allow_cancel_reason and "CancelReason" in payload:
        return ["CancelReason is only valid for cancel requests"]
    return []


def _gateway_bad_gateway(handler: BaseHTTPRequestHandler) -> None:
    _json_response(
        handler,
        HTTPStatus.BAD_GATEWAY,
        {
            "error": "bad_gateway",
            "message": "MWL API is unavailable",
        },
    )


def _proxy_mwl_response(handler: BaseHTTPRequestHandler, status_code: int, payload: Any) -> None:
    try:
        status = HTTPStatus(status_code)
    except ValueError:
        status = HTTPStatus.BAD_GATEWAY
    _json_response(handler, status, payload)


def _remote_ip(handler: BaseHTTPRequestHandler) -> str:
    try:
        return str(handler.client_address[0])
    except (AttributeError, IndexError, TypeError):
        return ""


def _unauthorized_response(handler: BaseHTTPRequestHandler) -> None:
    _json_response(handler, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})


def _audit_event(
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


def _workflow_event_type(path: str) -> str:
    if path == "/worklist":
        return "worklist_get"
    if path == "/worklist/cancel":
        return "worklist_cancel"
    if path == "/worklist/complete":
        return "worklist_complete"
    return "worklist_put"


def make_handler(config: GatewayConfig):
    class GatewayHandler(BaseHTTPRequestHandler):
        server_version = "KaosPACSGateway/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("Gateway API %s", format % args)

        def _mwl_client(self) -> MwlApiClient:
            return MwlApiClient(
                self.config.mwl_api_url,
                self.config.mwl_api_timeout_seconds,
            )

        def _require_auth(self, method: str, path: str) -> bool:
            if (method, path) not in PROTECTED_ENDPOINTS:
                return True

            if is_authorized(self.headers, self.config.gateway_api_token):
                if is_auth_enabled(self.config.gateway_api_token):
                    LOGGER.info(
                        "authentication success endpoint=%s remote_ip=%s",
                        path,
                        _remote_ip(self),
                    )
                return True

            LOGGER.warning(
                "authentication failed endpoint=%s remote_ip=%s",
                path,
                _remote_ip(self),
            )
            _unauthorized_response(self)
            return False

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                _json_response(self, HTTPStatus.OK, health_payload())
                return
            if not self._require_auth("GET", path):
                return
            if path == "/status":
                _json_response(self, HTTPStatus.OK, status_payload(self.config))
                return
            if path == "/worklist":
                try:
                    response = self._mwl_client().get_worklist()
                except MwlHttpError as error:
                    _audit_event(
                        self,
                        event_type="worklist_get",
                        request_path=path,
                        status_code=error.status_code,
                        success=False,
                        error_code="mwl_error",
                    )
                    _proxy_mwl_response(self, error.status_code, error.payload)
                    return
                except MwlUnavailableError:
                    _audit_event(
                        self,
                        event_type="mwl_unavailable",
                        request_path=path,
                        status_code=HTTPStatus.BAD_GATEWAY,
                        success=False,
                        error_code="mwl_unavailable",
                    )
                    _gateway_bad_gateway(self)
                    return
                _audit_event(
                    self,
                    event_type="worklist_get",
                    request_path=path,
                    status_code=response.status_code,
                    success=response.status_code < 400,
                )
                _proxy_mwl_response(self, response.status_code, response.payload)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if path != "/worklist":
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._require_auth("PUT", path):
                return

            try:
                payload = _read_json_request(self)
            except json.JSONDecodeError as error:
                _audit_event(
                    self,
                    event_type="validation_error",
                    request_path=path,
                    status_code=HTTPStatus.BAD_REQUEST,
                    success=False,
                    error_code="invalid_json",
                )
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid JSON", "details": [str(error)]},
                )
                return

            errors = _validate_worklist_payload(payload)
            if errors:
                _audit_event(
                    self,
                    event_type="validation_error",
                    request_path=path,
                    status_code=HTTPStatus.BAD_REQUEST,
                    success=False,
                    error_code="invalid_worklist",
                )
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid worklist", "details": errors},
                )
                return

            try:
                response = self._mwl_client().put_worklist(payload)
            except MwlHttpError as error:
                _audit_event(
                    self,
                    event_type="worklist_put",
                    request_path=path,
                    status_code=error.status_code,
                    success=False,
                    error_code="mwl_error",
                )
                _proxy_mwl_response(self, error.status_code, error.payload)
                return
            except MwlUnavailableError:
                _audit_event(
                    self,
                    event_type="mwl_unavailable",
                    request_path=path,
                    status_code=HTTPStatus.BAD_GATEWAY,
                    success=False,
                    error_code="mwl_unavailable",
                )
                _gateway_bad_gateway(self)
                return
            _audit_event(
                self,
                event_type="worklist_put",
                request_path=path,
                status_code=response.status_code,
                success=response.status_code < 400,
            )
            _proxy_mwl_response(self, response.status_code, response.payload)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if not self._require_auth("POST", path):
                return
            if path in {"/orders/upsert", "/orders/cancel"}:
                self._handle_order_post(path)
                return

            if path not in {"/worklist/complete", "/worklist/cancel"}:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            try:
                payload = _read_json_request(self)
            except json.JSONDecodeError as error:
                _audit_event(
                    self,
                    event_type="validation_error",
                    request_path=path,
                    status_code=HTTPStatus.BAD_REQUEST,
                    success=False,
                    error_code="invalid_json",
                )
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid JSON", "details": [str(error)]},
                )
                return

            allow_cancel_reason = path == "/worklist/cancel"
            errors = _validate_state_request(payload, allow_cancel_reason)
            accession_number = _text(payload.get("AccessionNumber")) if isinstance(payload, dict) else ""
            if errors:
                _audit_event(
                    self,
                    event_type="validation_error",
                    request_path=path,
                    accession_number=accession_number or None,
                    status_code=HTTPStatus.BAD_REQUEST,
                    success=False,
                    error_code="invalid_request",
                )
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid request", "details": errors},
                )
                return

            try:
                if path == "/worklist/complete":
                    response = self._mwl_client().complete_worklist(payload)
                else:
                    response = self._mwl_client().cancel_worklist(payload)
            except MwlHttpError as error:
                _audit_event(
                    self,
                    event_type=_workflow_event_type(path),
                    request_path=path,
                    accession_number=accession_number,
                    status_code=error.status_code,
                    success=False,
                    error_code="mwl_error",
                )
                _proxy_mwl_response(self, error.status_code, error.payload)
                return
            except MwlUnavailableError:
                _audit_event(
                    self,
                    event_type="mwl_unavailable",
                    request_path=path,
                    accession_number=accession_number,
                    status_code=HTTPStatus.BAD_GATEWAY,
                    success=False,
                    error_code="mwl_unavailable",
                )
                _gateway_bad_gateway(self)
                return
            _audit_event(
                self,
                event_type=_workflow_event_type(path),
                request_path=path,
                accession_number=accession_number,
                status_code=response.status_code,
                success=response.status_code < 400,
            )
            _proxy_mwl_response(self, response.status_code, response.payload)

        def _handle_order_post(self, path: str) -> None:
            try:
                payload = _read_json_request(self)
            except json.JSONDecodeError as error:
                _audit_event(
                    self,
                    event_type="validation_error",
                    request_path=path,
                    status_code=HTTPStatus.BAD_REQUEST,
                    success=False,
                    error_code="invalid_json",
                )
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid JSON", "details": [str(error)]},
                )
                return

            if path == "/orders/upsert":
                self._handle_order_upsert(path, payload)
                return
            self._handle_order_cancel(path, payload)

        def _handle_order_upsert(self, path: str, payload: Any) -> None:
            accession_number = (
                order_text(payload.get("AccessionNumber")) if isinstance(payload, dict) else ""
            )
            errors = validate_order_upsert(payload)
            if errors:
                _audit_event(
                    self,
                    event_type="validation_error",
                    request_path=path,
                    accession_number=accession_number or None,
                    status_code=HTTPStatus.BAD_REQUEST,
                    success=False,
                    error_code="invalid_order",
                )
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid order", "details": errors},
                )
                return

            entry = order_to_mwl_entry(payload)
            client = self._mwl_client()
            try:
                current_worklist = client.get_worklist()
                current_worklist_errors = _validate_worklist_payload(current_worklist.payload)
                if current_worklist_errors:
                    _audit_event(
                        self,
                        event_type="order_upsert",
                        request_path=path,
                        accession_number=accession_number,
                        status_code=HTTPStatus.BAD_GATEWAY,
                        success=False,
                        error_code="invalid_mwl_worklist",
                    )
                    _json_response(
                        self,
                        HTTPStatus.BAD_GATEWAY,
                        {
                            "error": "bad_gateway",
                            "message": "MWL API returned an invalid worklist",
                        },
                    )
                    return
                updated_worklist = upsert_worklist_entry(current_worklist.payload, entry)
                response = client.put_worklist(updated_worklist)
            except MwlHttpError as error:
                _audit_event(
                    self,
                    event_type="order_upsert",
                    request_path=path,
                    accession_number=accession_number,
                    status_code=error.status_code,
                    success=False,
                    error_code="mwl_error",
                )
                _proxy_mwl_response(self, error.status_code, error.payload)
                return
            except MwlUnavailableError:
                _audit_event(
                    self,
                    event_type="order_upsert",
                    request_path=path,
                    accession_number=accession_number,
                    status_code=HTTPStatus.BAD_GATEWAY,
                    success=False,
                    error_code="mwl_unavailable",
                )
                _gateway_bad_gateway(self)
                return

            _audit_event(
                self,
                event_type="order_upsert",
                request_path=path,
                accession_number=accession_number,
                status_code=response.status_code,
                success=response.status_code < 400,
            )
            _json_response(
                self,
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "action": "upserted",
                    "AccessionNumber": accession_number,
                },
            )

        def _handle_order_cancel(self, path: str, payload: Any) -> None:
            accession_number = (
                order_text(payload.get("AccessionNumber")) if isinstance(payload, dict) else ""
            )
            errors = validate_order_cancel(payload)
            if errors:
                _audit_event(
                    self,
                    event_type="validation_error",
                    request_path=path,
                    accession_number=accession_number or None,
                    status_code=HTTPStatus.BAD_REQUEST,
                    success=False,
                    error_code="invalid_order_cancel",
                )
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid order cancel", "details": errors},
                )
                return

            try:
                response = self._mwl_client().cancel_worklist(payload)
            except MwlHttpError as error:
                _audit_event(
                    self,
                    event_type="order_cancel",
                    request_path=path,
                    accession_number=accession_number,
                    status_code=error.status_code,
                    success=False,
                    error_code="mwl_error",
                )
                _proxy_mwl_response(self, error.status_code, error.payload)
                return
            except MwlUnavailableError:
                _audit_event(
                    self,
                    event_type="order_cancel",
                    request_path=path,
                    accession_number=accession_number,
                    status_code=HTTPStatus.BAD_GATEWAY,
                    success=False,
                    error_code="mwl_unavailable",
                )
                _gateway_bad_gateway(self)
                return

            _audit_event(
                self,
                event_type="order_cancel",
                request_path=path,
                accession_number=accession_number,
                status_code=response.status_code,
                success=response.status_code < 400,
            )
            _proxy_mwl_response(self, response.status_code, response.payload)

    GatewayHandler.config = config
    return GatewayHandler


def create_server(config: GatewayConfig) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        (config.http_host, config.http_port),
        make_handler(config),
    )


def main() -> None:
    config = load_config()
    configure_logging(config.log_level)
    LOGGER.info("Starting KaosPACS Gateway config=%s", config.safe_log_dict())
    if not is_auth_enabled(config.gateway_api_token):
        LOGGER.warning(
            "GATEWAY_API_TOKEN is not configured; Gateway authentication is disabled for development"
        )
    init_audit_db(config.gateway_audit_db)
    server = create_server(config)
    LOGGER.info("Gateway listening host=%s port=%s", config.http_host, config.http_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
