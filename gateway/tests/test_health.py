import json
import threading
from urllib.request import urlopen

from app.config import GatewayConfig
from app.health import health_payload
from app.main import create_server


def test_health_payload() -> None:
    assert health_payload() == {
        "status": "ok",
        "service": "gateway",
        "version": "0.1",
    }


def test_health_endpoint() -> None:
    config = GatewayConfig(http_host="127.0.0.1", http_port=0)
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)

    try:
        thread.start()
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/health", timeout=2) as response:
            body = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        assert body == health_payload()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
