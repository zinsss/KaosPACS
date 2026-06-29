import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.clients.orthanc import OrthancHttpClient


class OrthancHandler(BaseHTTPRequestHandler):
    response_status = HTTPStatus.OK
    response_payload = {
        "Name": "KaosPACS",
        "Version": "1.12.0",
        "PatientName": "SHOULD^NOTLOG",
    }

    def log_message(self, format, *args):
        return

    def do_GET(self):
        body = json.dumps(self.response_payload).encode("utf-8")
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_orthanc_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), OrthancHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_orthanc_client_get_system_success(caplog) -> None:
    caplog.set_level("INFO")
    server, thread = start_orthanc_server()
    host, port = server.server_address
    client = OrthancHttpClient(f"http://{host}:{port}", timeout_seconds=1)

    try:
        result = client.get_system()

        assert result.reachable is True
        assert result.status_code == 200
        assert result.payload["Name"] == "KaosPACS"
        assert client.is_reachable() == {
            "url": f"http://{host}:{port}",
            "reachable": True,
            "status_code": 200,
        }
        assert "PatientName" not in caplog.text
        assert "SHOULD^NOTLOG" not in caplog.text
    finally:
        stop_server(server, thread)


def test_orthanc_client_unavailable_returns_safe_error() -> None:
    client = OrthancHttpClient("http://127.0.0.1:1", timeout_seconds=0.2)

    result = client.get_system()

    assert result.reachable is False
    assert result.status_code is None
    assert result.payload is None
    assert result.error == "orthanc_unavailable"
    assert client.is_reachable() == {
        "url": "http://127.0.0.1:1",
        "reachable": False,
        "status_code": None,
    }
