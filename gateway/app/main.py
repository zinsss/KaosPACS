from __future__ import annotations

import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from app.api import json_response
from app.api.admin import handle_admin_worklist_prune
from app.api.imaging import handle_get_imaging_worklist
from app.api.orders import handle_order_post
from app.api.status import status_payload
from app.api.worklist import (
    handle_get_worklist,
    handle_put_worklist,
    handle_worklist_state_post,
)
from app.config import GatewayConfig, load_config
from app.dicom.queue import init_queue_db
from app.dicom.retry_worker import start_queue_retry_worker
from app.dicom.server import start_dicom_listener
from app.health import health_payload
from app.services.audit import init_audit_db
from app.services.auth import is_auth_enabled, is_authorized


LOGGER = logging.getLogger("kaospacs.gateway")
PROTECTED_ENDPOINTS = {
    ("GET", "/status"),
    ("GET", "/imaging/worklist"),
    ("GET", "/worklist"),
    ("PUT", "/worklist"),
    ("POST", "/worklist/complete"),
    ("POST", "/worklist/cancel"),
    ("POST", "/orders/upsert"),
    ("POST", "/orders/cancel"),
    ("POST", "/admin/worklist/prune"),
}


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _remote_ip(handler: BaseHTTPRequestHandler) -> str:
    try:
        return str(handler.client_address[0])
    except (AttributeError, IndexError, TypeError):
        return ""


def _unauthorized_response(handler: BaseHTTPRequestHandler) -> None:
    json_response(handler, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})


def make_handler(config: GatewayConfig):
    class GatewayHandler(BaseHTTPRequestHandler):
        server_version = "KaosPACSGateway/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("Gateway API %s", format % args)

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
                json_response(self, HTTPStatus.OK, health_payload())
                return
            if not self._require_auth("GET", path):
                return
            if path == "/status":
                json_response(self, HTTPStatus.OK, status_payload(self.config))
                return
            if path == "/imaging/worklist":
                handle_get_imaging_worklist(self, path)
                return
            if path == "/worklist":
                handle_get_worklist(self, path)
                return
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if path != "/worklist":
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._require_auth("PUT", path):
                return
            handle_put_worklist(self, path)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if not self._require_auth("POST", path):
                return
            if path == "/admin/worklist/prune":
                handle_admin_worklist_prune(self, path)
                return
            if path in {"/orders/upsert", "/orders/cancel"}:
                handle_order_post(self, path)
                return
            if path in {"/worklist/complete", "/worklist/cancel"}:
                handle_worklist_state_post(self, path)
                return
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

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
    init_queue_db(config.gateway_queue_db)
    retry_worker = start_queue_retry_worker(config)
    server = create_server(config)
    dicom_server = start_dicom_listener(config)
    LOGGER.info("Gateway listening host=%s port=%s", config.http_host, config.http_port)
    try:
        server.serve_forever()
    finally:
        if dicom_server is not None:
            dicom_server.stop()
        if retry_worker is not None:
            retry_worker.stop()


if __name__ == "__main__":
    main()
