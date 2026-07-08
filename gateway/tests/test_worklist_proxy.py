import json
import sqlite3
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from app.config import GatewayConfig
from app.dicom.queue import init_queue_db
from app.main import create_server
from app.services.audit import init_audit_db


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


def request_json(
    method: str,
    url: str,
    payload: Any | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    data = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def gateway_base_url(
    mwl_base_url: str,
    audit_db=None,
    gateway_queue_db=None,
    gateway_api_token: str | None = None,
    orthanc_url: str = "http://orthanc:8042",
) -> tuple[str, ThreadingHTTPServer, threading.Thread]:
    config_kwargs = {}
    if gateway_queue_db is not None:
        config_kwargs["gateway_queue_db"] = gateway_queue_db
    config = GatewayConfig(
        http_host="127.0.0.1",
        http_port=0,
        orthanc_url=orthanc_url,
        mwl_api_url=mwl_base_url,
        mwl_api_timeout_seconds=0.5,
        gateway_audit_db=audit_db,
        gateway_api_token=gateway_api_token,
        **config_kwargs,
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


FORBIDDEN_STATUS_TEXT = {
    "PatientName",
    "PatientBirthDate",
    "PatientSex",
    "ChartNo",
    "PatientID",
    "AccessionNumber",
    "DOB",
    "diagnosis",
    "EMR",
    "payload",
    "secret-token",
}


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


def test_health_works_without_token_when_auth_enabled(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json("GET", f"{gateway_url}/health")

        assert status == 200
        assert body == {"status": "ok", "service": "gateway", "version": "0.1"}
        assert RecordingMwlHandler.calls == []
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_protected_endpoint_without_token_returns_401(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json("GET", f"{gateway_url}/worklist")

        assert status == 401
        assert body == {"error": "unauthorized"}
        assert RecordingMwlHandler.calls == []
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/worklist", None),
        ("GET", "/imaging/worklist", None),
        ("GET", "/status", None),
        ("PUT", "/worklist", {"entries": []}),
        ("POST", "/worklist/complete", {"AccessionNumber": "A1"}),
        ("POST", "/worklist/cancel", {"AccessionNumber": "A1"}),
        ("POST", "/orders/upsert", valid_order_payload(AccessionNumber="A1")),
        ("POST", "/orders/cancel", {"AccessionNumber": "A1"}),
        ("POST", "/admin/worklist/prune", {}),
    ],
)
def test_all_workflow_endpoints_require_token(tmp_path, method, path, payload) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(method, f"{gateway_url}{path}", payload)

        assert status == 401
        assert body == {"error": "unauthorized"}
        assert RecordingMwlHandler.calls == []
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_protected_endpoint_wrong_token_returns_401(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/worklist",
            headers={"Authorization": "Bearer wrong-token"},
        )

        assert status == 401
        assert body == {"error": "unauthorized"}
        assert RecordingMwlHandler.calls == []
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_protected_endpoint_correct_token_succeeds(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"entries": []})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/worklist",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body == {"entries": []}
        assert RecordingMwlHandler.calls == [{"method": "GET", "path": "/worklist", "payload": None}]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def imaging_worklist_payload():
    return {
        "entries": [
            {
                "Active": False,
                "AccessionNumber": "COMPLETE-1",
                "PatientID": "P-COMPLETE",
                "PatientName": "DONE^PATIENT",
                "PatientBirthDate": "19700101",
                "PatientSex": "O",
                "Modality": "BMD",
                "ScheduledStationAETitle": "BMD",
                "ScheduledProcedureStepStartDate": "20260629",
                "ScheduledProcedureStepStartTime": "100000",
                "ScheduledProcedureStepDescription": "BMD COMPLETE",
                "CompletedAt": "2026-06-29T10:30:00+09:00",
            },
            {
                "Active": True,
                "AccessionNumber": "ACTIVE-2",
                "PatientID": "P-ACTIVE-2",
                "PatientName": "ACTIVE^TWO",
                "PatientBirthDate": "19800202",
                "PatientSex": "F",
                "Modality": "CR",
                "ScheduledStationAETitle": "INNOVISION",
                "ScheduledProcedureStepStartDate": "20260629",
                "ScheduledProcedureStepStartTime": "090000",
                "ScheduledProcedureStepDescription": "CR ACTIVE",
            },
            {
                "Active": False,
                "AccessionNumber": "EXPIRED-1",
                "PatientID": "P-EXPIRED",
                "PatientName": "OLD^PATIENT",
                "PatientBirthDate": "19600101",
                "PatientSex": "M",
                "Modality": "BMD",
                "ScheduledStationAETitle": "BMD",
                "ScheduledProcedureStepStartDate": "20260628",
                "ScheduledProcedureStepStartTime": "090000",
                "StudyDescription": "BMD EXPIRED",
                "ExpiredAt": "2026-06-29T23:59:59+09:00",
                "ExpireReason": "expired_without_imaging",
            },
            {
                "Active": False,
                "AccessionNumber": "CANCELLED-1",
                "PatientID": "P-CANCEL",
                "PatientName": "CANCEL^PATIENT",
                "PatientBirthDate": "19900101",
                "PatientSex": "O",
                "Modality": "BMD",
                "ScheduledStationAETitle": "BMD",
                "ScheduledProcedureStepStartDate": "20260630",
                "ScheduledProcedureStepStartTime": "110000",
                "RequestedProcedureDescription": "BMD CANCELLED",
                "CancelledAt": "2026-06-30T08:00:00+09:00",
                "CancelReason": "cancelled_in_source",
            },
            {
                "Active": False,
                "AccessionNumber": "INACTIVE-1",
                "PatientID": "P-INACTIVE",
                "PatientName": "INACTIVE^PATIENT",
                "PatientBirthDate": "",
                "PatientSex": "O",
                "Modality": "OT",
                "ScheduledStationAETitle": "BMD",
                "ScheduledProcedureStepStartDate": "",
                "ScheduledProcedureStepStartTime": "",
                "ScheduledProcedureStepDescription": "",
            },
            {
                "Active": True,
                "AccessionNumber": "ACTIVE-1",
                "PatientID": "P-ACTIVE-1",
                "PatientName": "ACTIVE^ONE",
                "PatientBirthDate": "19810101",
                "PatientSex": "M",
                "Modality": "BMD",
                "ScheduledStationAETitle": "BMD",
                "ScheduledProcedureStepStartDate": "20260629",
                "ScheduledProcedureStepStartTime": "080000",
                "ScheduledProcedureStepDescription": "BMD ACTIVE",
            },
        ]
    }


def test_imaging_worklist_maps_states_counts_and_calls_only_mwl_worklist(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(imaging_worklist_payload())
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/imaging/worklist",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["counts"] == {
            "active": 2,
            "completed": 1,
            "expired": 1,
            "cancelled": 1,
        }
        assert [entry["AccessionNumber"] for entry in body["entries"]] == [
            "ACTIVE-1",
            "ACTIVE-2",
            "CANCELLED-1",
            "EXPIRED-1",
            "COMPLETE-1",
        ]
        entries = {entry["AccessionNumber"]: entry for entry in body["entries"]}
        assert entries["ACTIVE-1"] == {
            "state": "active",
            "AccessionNumber": "ACTIVE-1",
            "PatientID": "P-ACTIVE-1",
            "PatientName": "ACTIVE^ONE",
            "PatientBirthDate": "19810101",
            "PatientSex": "M",
            "Modality": "BMD",
            "ScheduledStationAETitle": "BMD",
            "ScheduledAt": "2026-06-29T08:00:00",
            "Description": "BMD ACTIVE",
            "CompletedAt": None,
            "ExpiredAt": None,
            "ExpireReason": None,
            "CancelledAt": None,
            "CancelReason": None,
        }
        assert entries["COMPLETE-1"]["state"] == "completed"
        assert entries["COMPLETE-1"]["CompletedAt"] == "2026-06-29T10:30:00+09:00"
        assert entries["EXPIRED-1"]["state"] == "expired"
        assert entries["EXPIRED-1"]["ExpiredAt"] == "2026-06-29T23:59:59+09:00"
        assert entries["EXPIRED-1"]["ExpireReason"] == "expired_without_imaging"
        assert entries["CANCELLED-1"]["state"] == "cancelled"
        assert entries["CANCELLED-1"]["CancelReason"] == "cancelled_in_source"
        assert "INACTIVE-1" not in entries
        assert RecordingMwlHandler.calls == [
            {"method": "GET", "path": "/worklist", "payload": None}
        ]
        assert audit_rows(audit_db) == [
            ("imaging_worklist_get", "/imaging/worklist", None, 200, 1, None)
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_imaging_worklist_view_all_includes_inactive_without_marking_active(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(imaging_worklist_payload())
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/imaging/worklist?view=all",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["counts"] == {
            "active": 2,
            "completed": 1,
            "expired": 1,
            "cancelled": 1,
            "inactive": 1,
        }
        entries = {entry["AccessionNumber"]: entry for entry in body["entries"]}
        assert entries["INACTIVE-1"]["state"] == "inactive"
        assert entries["INACTIVE-1"]["CompletedAt"] is None
        assert entries["INACTIVE-1"]["ExpiredAt"] is None
        assert entries["INACTIVE-1"]["CancelledAt"] is None
        assert RecordingMwlHandler.calls == [
            {"method": "GET", "path": "/worklist", "payload": None}
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_authentication_disabled_when_token_unset(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"entries": []})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token=None,
    )

    try:
        status, body = request_json("GET", f"{gateway_url}/worklist")

        assert status == 200
        assert body == {"entries": []}
        assert RecordingMwlHandler.calls == [{"method": "GET", "path": "/worklist", "payload": None}]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_token_not_present_in_logs(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl()
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="super-secret-token",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/worklist",
            headers={"Authorization": "Bearer wrong-super-secret-token"},
        )

        assert status == 401
        assert body == {"error": "unauthorized"}
        assert "authentication failed" in caplog.text
        assert "/worklist" in caplog.text
        assert "super-secret-token" not in caplog.text
        assert "wrong-super-secret-token" not in caplog.text
        assert "Authorization" not in caplog.text
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_status_without_token_returns_401_when_auth_enabled(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"status": "ok"})
    orthanc_server, orthanc_thread = setup_recording_mwl({"Name": "Orthanc"})
    mwl_host, mwl_port = mwl_server.server_address
    orthanc_host, orthanc_port = orthanc_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
        orthanc_url=f"http://{orthanc_host}:{orthanc_port}",
    )

    try:
        status, body = request_json("GET", f"{gateway_url}/status")

        assert status == 401
        assert body == {"error": "unauthorized"}
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)
        stop_server(orthanc_server, orthanc_thread)


def test_status_with_token_returns_operational_metadata(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    queue_db = tmp_path / "gateway_queue.sqlite3"
    init_audit_db(audit_db)
    init_queue_db(queue_db)
    mwl_server, mwl_thread = setup_recording_mwl({"status": "ok"})
    orthanc_server, orthanc_thread = setup_recording_mwl({"Name": "Orthanc"})
    mwl_host, mwl_port = mwl_server.server_address
    orthanc_host, orthanc_port = orthanc_server.server_address
    mwl_url = f"http://{mwl_host}:{mwl_port}"
    orthanc_url = f"http://{orthanc_host}:{orthanc_port}"
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        mwl_url,
        audit_db,
        gateway_queue_db=queue_db,
        gateway_api_token="secret-token",
        orthanc_url=orthanc_url,
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/status",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["status"] == "ok"
        assert body["service"] == "gateway"
        assert body["version"] == "0.1"
        assert body["auth"] == {"enabled": True}
        assert body["dependencies"]["mwl_api"] == {
            "url": mwl_url,
            "reachable": True,
            "status_code": 200,
        }
        assert body["dependencies"]["orthanc_http"] == {
            "url": orthanc_url,
            "reachable": True,
            "status_code": 200,
        }
        assert body["dependencies"]["gateway_audit_db"] == {
            "path": str(audit_db),
            "reachable": True,
        }
        assert body["gateway_dicom"] == {
            "enabled": True,
            "aet": "VIEWREX",
            "bind": "0.0.0.0",
            "port": 104,
            "storage_dir": "/app/data/dicom-inbox",
            "queue_enabled": False,
            "queue_db": {
                "path": str(queue_db),
                "reachable": True,
            },
            "queue_counts": {
                "pending": 0,
                "forwarding": 0,
                "completed": 0,
                "failed": 0,
                "dead_letter": 0,
            },
            "worker": {
                "enabled": False,
                "running": False,
                "poll_interval_seconds": 5.0,
                "max_attempts": 10,
            },
            "forward_mode": "direct",
            "forward_enabled": True,
            "forward_target": {
                "host": "orthanc",
                "port": 11112,
                "aet": "VIEWREX",
            },
            "inspection": {
                "enabled": True,
                "report_path": "/app/data/dicom_inspection.jsonl",
            },
            "charset_fix": {
                "enabled": True,
                "mode": "iso_ir_149_to_utf8",
                "report_path": "/app/data/dicom_charset_fix.jsonl",
            },
            "mode": "production-front-door",
        }
        assert body["ownership"]["storage_scp"] == {
            "aet": "VIEWREX",
            "port": 104,
            "owner": "gateway",
            "stage": "production",
        }
        assert body["ownership"]["storage_backend"] == {
            "owner": "orthanc",
            "dicom_host": "orthanc",
            "dicom_port": 11112,
            "dicom_aet": "VIEWREX",
            "stage": "internal",
        }
        assert body["ownership"]["mwl_scp"] == {
            "aet": "VIEWREX_WL",
            "port": 105,
            "owner": "mwl",
            "stage": "current-final",
        }
        assert body["ownership"]["gateway_http"]["owner"] == "gateway"
        assert "PatientName" not in str(body)
        assert "PatientID" not in str(body)
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)
        stop_server(orthanc_server, orthanc_thread)


def test_status_without_token_works_when_auth_disabled(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)
    mwl_server, mwl_thread = setup_recording_mwl({"status": "ok"})
    orthanc_server, orthanc_thread = setup_recording_mwl({"Name": "Orthanc"})
    mwl_host, mwl_port = mwl_server.server_address
    orthanc_host, orthanc_port = orthanc_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token=None,
        orthanc_url=f"http://{orthanc_host}:{orthanc_port}",
    )

    try:
        status, body = request_json("GET", f"{gateway_url}/status")

        assert status == 200
        assert body["auth"] == {"enabled": False}
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)
        stop_server(orthanc_server, orthanc_thread)


def test_status_does_not_include_phi_or_token(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)
    mwl_server, mwl_thread = setup_recording_mwl({"status": "ok"})
    orthanc_server, orthanc_thread = setup_recording_mwl({"Name": "Orthanc"})
    mwl_host, mwl_port = mwl_server.server_address
    orthanc_host, orthanc_port = orthanc_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
        orthanc_url=f"http://{orthanc_host}:{orthanc_port}",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/status",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        serialized = json.dumps(body)
        for forbidden in FORBIDDEN_STATUS_TEXT:
            assert forbidden not in serialized
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)
        stop_server(orthanc_server, orthanc_thread)


def test_status_reports_mwl_unavailable_without_crashing(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)
    orthanc_server, orthanc_thread = setup_recording_mwl({"Name": "Orthanc"})
    orthanc_host, orthanc_port = orthanc_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        "http://127.0.0.1:1",
        audit_db,
        gateway_api_token="secret-token",
        orthanc_url=f"http://{orthanc_host}:{orthanc_port}",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/status",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["dependencies"]["mwl_api"]["reachable"] is False
        assert body["dependencies"]["mwl_api"]["status_code"] is None
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(orthanc_server, orthanc_thread)


def test_status_reports_orthanc_unavailable_without_crashing(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)
    mwl_server, mwl_thread = setup_recording_mwl({"status": "ok"})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
        orthanc_url="http://127.0.0.1:1",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/status",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["dependencies"]["orthanc_http"]["reachable"] is False
        assert body["dependencies"]["orthanc_http"]["status_code"] is None
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_status_reports_audit_db_unavailable(tmp_path) -> None:
    mwl_server, mwl_thread = setup_recording_mwl({"status": "ok"})
    orthanc_server, orthanc_thread = setup_recording_mwl({"Name": "Orthanc"})
    mwl_host, mwl_port = mwl_server.server_address
    orthanc_host, orthanc_port = orthanc_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        tmp_path,
        gateway_api_token="secret-token",
        orthanc_url=f"http://{orthanc_host}:{orthanc_port}",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/status",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["dependencies"]["gateway_audit_db"] == {
            "path": str(tmp_path),
            "reachable": False,
        }
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)
        stop_server(orthanc_server, orthanc_thread)


def test_status_reports_queue_db_unavailable(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    init_audit_db(audit_db)
    mwl_server, mwl_thread = setup_recording_mwl({"status": "ok"})
    orthanc_server, orthanc_thread = setup_recording_mwl({"Name": "Orthanc"})
    mwl_host, mwl_port = mwl_server.server_address
    orthanc_host, orthanc_port = orthanc_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_queue_db=tmp_path,
        gateway_api_token="secret-token",
        orthanc_url=f"http://{orthanc_host}:{orthanc_port}",
    )

    try:
        status, body = request_json(
            "GET",
            f"{gateway_url}/status",
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["gateway_dicom"]["queue_db"] == {
            "path": str(tmp_path),
            "reachable": False,
        }
        assert body["gateway_dicom"]["queue_counts"] is None
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)
        stop_server(orthanc_server, orthanc_thread)


def prune_worklist_payload():
    return {
        "entries": [
            {
                "Active": True,
                "AccessionNumber": "ACTIVE-1",
                "PatientName": "ACTIVE^PATIENT",
                "PatientID": "ACTIVE-CHART",
                "CompletedAt": "2026-06-01T09:00:00+09:00",
            },
            {
                "Active": False,
                "AccessionNumber": "COMPLETE-OLD",
                "PatientName": "COMPLETE^PATIENT",
                "PatientID": "COMPLETE-CHART",
                "CompletedAt": "2026-06-01T09:00:00+09:00",
            },
            {
                "Active": False,
                "AccessionNumber": "CANCEL-OLD",
                "PatientName": "CANCEL^PATIENT",
                "PatientID": "CANCEL-CHART",
                "CancelledAt": "2026-06-01T09:00:00+09:00",
            },
            {
                "Active": False,
                "AccessionNumber": "EXPIRED-OLD",
                "PatientName": "EXPIRED^PATIENT",
                "PatientID": "EXPIRED-CHART",
                "ExpiredAt": "2026-06-01T09:00:00+09:00",
                "ExpireReason": "expired_without_imaging",
            },
            {
                "Active": False,
                "AccessionNumber": "COMPLETE-NEW",
                "PatientName": "NEW^PATIENT",
                "PatientID": "NEW-CHART",
                "CompletedAt": datetime.now().astimezone().isoformat(),
            },
            {
                "Active": False,
                "AccessionNumber": "BAD-TIME",
                "PatientName": "BAD^PATIENT",
                "PatientID": "BAD-CHART",
                "CompletedAt": "not-a-date",
            },
        ]
    }


def test_admin_worklist_prune_default_dry_run_does_not_put(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(prune_worklist_payload())
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/admin/worklist/prune",
            {},
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["dry_run"] is True
        assert body["older_than_days"] == 7
        assert body["statuses"] == ["completed", "cancelled", "expired"]
        assert body["before_count"] == 6
        assert body["after_count"] == 3
        assert body["removed_count"] == 3
        assert body["removed"] == [
            {
                "AccessionNumber": "COMPLETE-OLD",
                "reason": "completed",
                "timestamp": "2026-06-01T09:00:00+09:00",
            },
            {
                "AccessionNumber": "CANCEL-OLD",
                "reason": "cancelled",
                "timestamp": "2026-06-01T09:00:00+09:00",
            },
            {
                "AccessionNumber": "EXPIRED-OLD",
                "reason": "expired",
                "timestamp": "2026-06-01T09:00:00+09:00",
            },
        ]
        assert RecordingMwlHandler.calls == [
            {"method": "GET", "path": "/worklist", "payload": None}
        ]
        serialized = json.dumps(body)
        assert "PatientName" not in serialized
        assert "PatientID" not in serialized
        assert audit_rows(audit_db) == [
            ("admin_worklist_prune", "/admin/worklist/prune", None, 200, 1, None)
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_admin_worklist_prune_false_puts_pruned_entries(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(prune_worklist_payload())
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/admin/worklist/prune",
            {"dry_run": False, "older_than_days": 7, "statuses": ["completed", "cancelled"]},
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["dry_run"] is False
        assert body["removed_count"] == 2
        assert [call["method"] for call in RecordingMwlHandler.calls] == ["GET", "PUT"]
        put_payload = RecordingMwlHandler.calls[1]["payload"]
        remaining_accessions = [
            entry["AccessionNumber"]
            for entry in put_payload["entries"]
        ]
        assert remaining_accessions == [
            "ACTIVE-1",
            "EXPIRED-OLD",
            "COMPLETE-NEW",
            "BAD-TIME",
        ]
        assert put_payload["entries"][0]["Active"] is True
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_admin_worklist_prune_expired_only_when_requested(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(prune_worklist_payload())
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/admin/worklist/prune",
            {"statuses": ["expired"], "older_than_days": 0},
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 200
        assert body["removed"] == [
            {
                "AccessionNumber": "EXPIRED-OLD",
                "reason": "expired",
                "timestamp": "2026-06-01T09:00:00+09:00",
            }
        ]
        assert RecordingMwlHandler.calls == [
            {"method": "GET", "path": "/worklist", "payload": None}
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


@pytest.mark.parametrize(
    ("payload", "expected_detail"),
    [
        ({"statuses": ["unknown"]}, "statuses must only contain: completed, cancelled, expired"),
        ({"older_than_days": -1}, "older_than_days must be an integer >= 0"),
        ({"older_than_days": True}, "older_than_days must be an integer >= 0"),
        ({"dry_run": "yes"}, "dry_run must be a boolean"),
    ],
)
def test_admin_worklist_prune_invalid_request_does_not_call_mwl(tmp_path, payload, expected_detail) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(prune_worklist_payload())
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/admin/worklist/prune",
            payload,
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 400
        assert expected_detail in body["details"]
        assert RecordingMwlHandler.calls == []
        assert audit_rows(audit_db) == [
            ("admin_worklist_prune", "/admin/worklist/prune", None, 400, 0, "invalid_request")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(mwl_server, mwl_thread)


def test_admin_worklist_prune_mwl_unavailable_returns_502(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        "http://127.0.0.1:1",
        audit_db,
        gateway_api_token="secret-token",
    )

    try:
        status, body = request_json(
            "POST",
            f"{gateway_url}/admin/worklist/prune",
            {},
            headers={"Authorization": "Bearer secret-token"},
        )

        assert status == 502
        assert body == {
            "error": "bad_gateway",
            "message": "MWL API is unavailable",
        }
        assert audit_rows(audit_db) == [
            ("admin_worklist_prune", "/admin/worklist/prune", None, 502, 0, "mwl_unavailable")
        ]
    finally:
        stop_server(gateway_server, gateway_thread)


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


def test_order_upsert_preserves_korean_text_and_sets_utf8_charset(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl({"entries": []})
    mwl_host, mwl_port = mwl_server.server_address
    gateway_url, gateway_server, gateway_thread = gateway_base_url(
        f"http://{mwl_host}:{mwl_port}",
        audit_db,
    )
    payload = valid_order_payload(
        ChartNo="PT-KR-001",
        PatientName="홍길동",
        AccessionNumber="ACC-KR-001",
        Description="골밀도 검사",
        StudyType="골밀도",
    )

    try:
        status, body = request_json("POST", f"{gateway_url}/orders/upsert", payload)

        assert status == 200
        assert body["AccessionNumber"] == "ACC-KR-001"
        put_call = RecordingMwlHandler.calls[1]
        assert put_call["method"] == "PUT"
        new_entry = put_call["payload"]["entries"][0]
        assert new_entry["PatientID"] == "PT-KR-001"
        assert new_entry["PatientName"] == "홍길동"
        assert new_entry["StudyDescription"] == "골밀도 검사"
        assert new_entry["RequestedProcedureDescription"] == "골밀도 검사"
        assert new_entry["ScheduledProcedureStepDescription"] == "골밀도 검사"
        assert new_entry["StudyType"] == "골밀도"
        assert new_entry["SpecificCharacterSet"] == "ISO_IR 192"
        serialized = json.dumps(put_call["payload"], ensure_ascii=False)
        assert "홍길동" in serialized
        assert "골밀도 검사" in serialized
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


def test_order_upsert_does_not_reactivate_completed_entry(tmp_path) -> None:
    audit_db = tmp_path / "gateway_audit.sqlite3"
    mwl_server, mwl_thread = setup_recording_mwl(
        {
            "entries": [
                {
                    "AccessionNumber": "A1",
                    "PatientID": "old",
                    "PatientName": "OLD^NAME",
                    "Modality": "CR",
                    "ScheduledStationAETitle": "INNOVISION",
                    "ScheduledProcedureStepDescription": "CHEST",
                    "Active": False,
                    "CompletedAt": "2026-07-08T09:56:11+09:00",
                }
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
            valid_order_payload(
                AccessionNumber="A1",
                PatientName="NEW^NAME",
                ChartNo="new-chart",
                Modality="CR",
                StationAET="INNOVISION",
                StudyType="CR",
                Description="CHEST",
            ),
        )

        assert status == 200
        entry = RecordingMwlHandler.calls[1]["payload"]["entries"][0]
        assert entry["AccessionNumber"] == "A1"
        assert entry["PatientID"] == "new-chart"
        assert entry["PatientName"] == "NEW^NAME"
        assert entry["Active"] is False
        assert entry["CompletedAt"] == "2026-07-08T09:56:11+09:00"
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
