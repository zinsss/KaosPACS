import json
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.config import GatewayConfig
from app.main import create_server


class RecordingMwlHandler(BaseHTTPRequestHandler):
    response_status = HTTPStatus.OK
    response_payload: dict[str, Any] = {"status": "ok"}
    calls: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _record(self, payload: Any = None) -> None:
        self.calls.append(
            {
                "method": self.command,
                "path": self.path,
                "payload": payload,
            }
        )

    def _respond(self) -> None:
        body = json.dumps(self.response_payload).encode("utf-8")
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._record()
        self._respond()

    def do_PUT(self) -> None:
        self._record(self._read_json())
        self._respond()

    def do_POST(self) -> None:
        self._record(self._read_json())
        self._respond()


def start_server(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request_json(method: str, url: str, payload: Any | None = None) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def gateway_base_url(
    mwl_base_url: str,
    audit_db=None,
) -> tuple[str, ThreadingHTTPServer, threading.Thread]:
    config = GatewayConfig(
        http_host="127.0.0.1",
        http_port=0,
        mwl_api_url=mwl_base_url,
        mwl_api_timeout_seconds=0.5,
        gateway_audit_db=audit_db,
    )
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return f"http://{host}:{port}", server, thread


def audit_rows(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT event_type, request_path, accession_number, status_code, success, error_code
            FROM gateway_events
            ORDER BY id
            """
        ).fetchall()


def setup_recording_mwl(response_payload: dict[str, Any] | None = None):
    RecordingMwlHandler.calls = []
    RecordingMwlHandler.response_status = HTTPStatus.OK
    RecordingMwlHandler.response_payload = response_payload or {"entries": []}
    return start_server(RecordingMwlHandler)


def valid_order_payload(**overrides):
    payload = {
        "ChartNo": "12345",
        "PatientName": "TEST^PATIENT",
        "PatientBirthDate": "19700101",
        "PatientSex": "O",
        "AccessionNumber": "20260629-12345-1",
        "StudyType": "BMD",
        "Modality": "BMD",
        "StationAET": "BMD",
        "ScheduledAt": "2026-06-29T09:00:00+09:00",
        "Description": "BMD",
        "ExpiresAt": "2026-06-29T23:59:59+09:00",
    }
    payload.update(overrides)
    return payload


def test_get_worklist_proxies_mwl_response(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"entries": [{"PatientID": "P1"}]})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )

    try:
        status, body = request_json("GET", f"{gateway_url}/worklist")

        assert status == 200
        assert body == {"entries": [{"PatientID": "P1"}]}
        assert RecordingMwlHandler.calls == [{"method": "GET", "path": "/worklist", "payload": None}]
        assert audit_rows(audit_db) == [
            ("worklist_get", "/worklist", None, 200, 1, None)
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_put_worklist_sends_valid_payload_to_mwl(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"entries": []})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )
    payload = {"entries": [{"PatientID": "P1", "AccessionNumber": "A1"}]}

    try:
        status, body = request_json("PUT", f"{gateway_url}/worklist", payload)

        assert status == 200
        assert body == {"entries": []}
        assert RecordingMwlHandler.calls == [
            {"method": "PUT", "path": "/worklist", "payload": payload}
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_invalid_put_returns_400_without_calling_mwl(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )

    try:
        status, body = request_json("PUT", f"{gateway_url}/worklist", {"entries": "bad"})

        assert status == 400
        assert body["error"] == "invalid worklist"
        assert RecordingMwlHandler.calls == []
        assert audit_rows(audit_db) == [
            ("validation_error", "/worklist", None, 400, 0, "invalid_worklist")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_complete_requires_accession_number(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )

    try:
        status, body = request_json("POST", f"{gateway_url}/worklist/complete", {})

        assert status == 400
        assert body["details"] == ["AccessionNumber is required"]
        assert RecordingMwlHandler.calls == []
        assert audit_rows(audit_db) == [
            ("validation_error", "/worklist/complete", None, 400, 0, "invalid_request")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_complete_forwards_accession_number_to_mwl(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"status": "completed"})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )
    payload = {"AccessionNumber": "A1"}

    try:
        status, body = request_json("POST", f"{gateway_url}/worklist/complete", payload)

        assert status == 200
        assert body == {"status": "completed"}
        assert RecordingMwlHandler.calls == [
            {"method": "POST", "path": "/worklist/complete", "payload": payload}
        ]
        assert audit_rows(audit_db) == [
            ("worklist_complete", "/worklist/complete", "A1", 200, 1, None)
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_cancel_requires_accession_number(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/worklist/cancel",
            {"CancelReason": "patient no-show"},
        )

        assert status == 400
        assert body["details"] == ["AccessionNumber is required"]
        assert RecordingMwlHandler.calls == []
        assert audit_rows(audit_db) == [
            ("validation_error", "/worklist/cancel", None, 400, 0, "invalid_request")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_cancel_forwards_accession_number_and_reason_to_mwl(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"status": "cancelled"})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )
    payload = {"AccessionNumber": "A1", "CancelReason": "patient no-show"}

    try:
        status, body = request_json("POST", f"{gateway_url}/worklist/cancel", payload)

        assert status == 200
        assert body == {"status": "cancelled"}
        assert RecordingMwlHandler.calls == [
            {"method": "POST", "path": "/worklist/cancel", "payload": payload}
        ]
        assert audit_rows(audit_db) == [
            ("worklist_cancel", "/worklist/cancel", "A1", 200, 1, None)
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_mwl_unavailable_returns_502(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        "http://127.0.0.1:1",
        audit_db,
    )

    try:
        status, body = request_json("GET", f"{gateway_url}/worklist")

        assert status == 502
        assert body == {
            "error": "bad_gateway",
            "message": "MWL API is unavailable",
        }
        assert audit_rows(audit_db) == [
            ("mwl_unavailable", "/worklist", None, 502, 0, "mwl_unavailable")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)


def test_audit_failure_does_not_break_request(tmp_path) -> None:
    mwl_server, mwl_thread = setup_recording_mwl({"entries": []})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        tmp_path,
    )

    try:
        status, body = request_json("GET", f"{gateway_url}/worklist")

        assert status == 200
        assert body == {"entries": []}
        assert RecordingMwlHandler.calls == [{"method": "GET", "path": "/worklist", "payload": None}]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_order_upsert_appends_worklist_entry(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(
        {"entries": [{"AccessionNumber": "A1", "PatientID": "keep"}]}
    )
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )
    payload = valid_order_payload(AccessionNumber="A2")

    try:
        status, body = request_json("POST", f"{gateway_url}/orders/upsert", payload)

        assert status == 200
        assert body == {
            "status": "ok",
            "action": "upserted",
            "AccessionNumber": "A2",
        }
        assert RecordingMwlHandler.calls[0] == {
            "method": "GET",
            "path": "/worklist",
            "payload": None,
        }
        put_call = RecordingMwlHandler.calls[1]
        assert put_call["method"] == "PUT"
        assert put_call["path"] == "/worklist"
        assert put_call["payload"]["entries"][0] == {"AccessionNumber": "A1", "PatientID": "keep"}
        new_entry = put_call["payload"]["entries"][1]
        assert new_entry["PatientID"] == "12345"
        assert new_entry["PatientName"] == "TEST^PATIENT"
        assert new_entry["AccessionNumber"] == "A2"
        assert new_entry["ScheduledProcedureStepStartDate"] == "20260629"
        assert new_entry["ScheduledProcedureStepStartTime"] == "090000"
        assert new_entry["SpecificCharacterSet"] == "ISO_IR 192"
        assert audit_rows(audit_db) == [
            ("order_upsert", "/orders/upsert", "A2", 200, 1, None)
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_order_upsert_replaces_matching_accession(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(
        {
            "entries": [
                {"AccessionNumber": "A1", "PatientID": "old"},
                {"AccessionNumber": "A2", "PatientID": "keep", "Active": False},
            ]
        }
    )
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )

    try:
        status, _body = request_json(
            "POST",
            f"{gateway_url}/orders/upsert",
            valid_order_payload(AccessionNumber="A1", ChartNo="new-chart"),
        )

        assert status == 200
        put_entries = RecordingMwlHandler.calls[1]["payload"]["entries"]
        assert put_entries[0]["AccessionNumber"] == "A1"
        assert put_entries[0]["PatientID"] == "new-chart"
        assert put_entries[0]["Active"] is True
        assert put_entries[1] == {"AccessionNumber": "A2", "PatientID": "keep", "Active": False}
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_order_upsert_invalid_request_does_not_call_mwl(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )

    try:
        status, body = request_json("POST", f"{gateway_url}/orders/upsert", {"ChartNo": "12345"})

        assert status == 400
        assert body["error"] == "invalid order"
        assert "PatientName is required" in body["details"]
        assert "AccessionNumber is required" in body["details"]
        assert RecordingMwlHandler.calls == []
        assert audit_rows(audit_db) == [
            ("validation_error", "/orders/upsert", None, 400, 0, "invalid_order")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_order_upsert_invalid_mwl_worklist_returns_502_without_put(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"status": "not a worklist"})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/orders/upsert",
            valid_order_payload(AccessionNumber="A1"),
        )

        assert status == 502
        assert body == {
            "error": "bad_gateway",
            "message": "MWL API returned an invalid worklist",
        }
        assert RecordingMwlHandler.calls == [
            {"method": "GET", "path": "/worklist", "payload": None}
        ]
        assert audit_rows(audit_db) == [
            ("order_upsert", "/orders/upsert", "A1", 502, 0, "invalid_mwl_worklist")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_order_cancel_calls_mwl_cancel(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"status": "cancelled"})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )
    payload = {"AccessionNumber": "A1", "CancelReason": "cancelled from eGHIS"}

    try:
        status, body = request_json("POST", f"{gateway_url}/orders/cancel", payload)

        assert status == 200
        assert body == {"status": "cancelled"}
        assert RecordingMwlHandler.calls == [
            {"method": "POST", "path": "/worklist/cancel", "payload": payload}
        ]
        assert audit_rows(audit_db) == [
            ("order_cancel", "/orders/cancel", "A1", 200, 1, None)
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_order_cancel_invalid_request_does_not_call_mwl(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/orders/cancel",
            {"CancelReason": "cancelled from eGHIS"},
        )

        assert status == 400
        assert body["details"] == ["AccessionNumber is required"]
        assert RecordingMwlHandler.calls == []
        assert audit_rows(audit_db) == [
            ("validation_error", "/orders/cancel", None, 400, 0, "invalid_order_cancel")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_order_upsert_mwl_unavailable_returns_502(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        "http://127.0.0.1:1",
        audit_db,
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/orders/upsert",
            valid_order_payload(AccessionNumber="A1"),
        )

        assert status == 502
        assert body == {
            "error": "bad_gateway",
            "message": "MWL API is unavailable",
        }
        assert audit_rows(audit_db) == [
            ("order_upsert", "/orders/upsert", "A1", 502, 0, "mwl_unavailable")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
