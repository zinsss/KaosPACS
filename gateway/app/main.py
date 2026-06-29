from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .config import GatewayConfig, load_config
from .health import health_payload


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


def make_handler(config: GatewayConfig):
    class GatewayHandler(BaseHTTPRequestHandler):
        server_version = "KaosPACSGateway/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("Gateway API %s", format % args)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                _json_response(self, HTTPStatus.OK, health_payload())
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

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
