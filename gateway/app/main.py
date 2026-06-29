from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .config import GatewayConfig, load_config
from .health import health_payload
from .mwl_client import MwlApiClient, MwlHttpError, MwlUnavailableError


LOGGER = logging.getLogger("kaospacs.gateway")


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

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                _json_response(self, HTTPStatus.OK, health_payload())
                return
            if path == "/worklist":
                try:
                    response = self._mwl_client().get_worklist()
                except MwlHttpError as error:
                    _proxy_mwl_response(self, error.status_code, error.payload)
                    return
                except MwlUnavailableError:
                    _gateway_bad_gateway(self)
                    return
                _proxy_mwl_response(self, response.status_code, response.payload)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if path != "/worklist":
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            try:
                payload = _read_json_request(self)
            except json.JSONDecodeError as error:
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid JSON", "details": [str(error)]},
                )
                return

            errors = _validate_worklist_payload(payload)
            if errors:
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid worklist", "details": errors},
                )
                return

            try:
                response = self._mwl_client().put_worklist(payload)
            except MwlHttpError as error:
                _proxy_mwl_response(self, error.status_code, error.payload)
                return
            except MwlUnavailableError:
                _gateway_bad_gateway(self)
                return
            _proxy_mwl_response(self, response.status_code, response.payload)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in {"/worklist/complete", "/worklist/cancel"}:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            try:
                payload = _read_json_request(self)
            except json.JSONDecodeError as error:
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid JSON", "details": [str(error)]},
                )
                return

            allow_cancel_reason = path == "/worklist/cancel"
            errors = _validate_state_request(payload, allow_cancel_reason)
            if errors:
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
                _proxy_mwl_response(self, error.status_code, error.payload)
                return
            except MwlUnavailableError:
                _gateway_bad_gateway(self)
                return
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
    server = create_server(config)
    LOGGER.info("Gateway listening host=%s port=%s", config.http_host, config.http_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
