from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mwl"))

from tests.test_main import audit_rows, start_test_api, valid_entry, write_worklist


def _load_gateway_create_app():
    module_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("kaospacs_gateway_app_main", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module.create_app


create_app = _load_gateway_create_app()


def test_admin_complete_requires_bearer_token() -> None:
    app = create_app(
        {
            "GATEWAY_API_TOKEN": "secret",
            "FETCH_WORKLIST": lambda: [valid_entry()],
        }
    )
    client = app.test_client()

    response = client.post(
        "/admin/worklist/complete",
        json={
            "AccessionNumber": "VALID001",
            "CompleteReason": "operator_verified_completed",
        },
    )

    assert response.status_code == 401


def test_active_row_can_be_admin_marked_completed() -> None:
    completed = []
    app = create_app(
        {
            "GATEWAY_API_TOKEN": "secret",
            "FETCH_WORKLIST": lambda: [valid_entry()],
            "COMPLETE_WORKLIST_ENTRY": lambda payload: completed.append(payload) or {"updated": 1},
        }
    )
    client = app.test_client()

    response = client.post(
        "/admin/worklist/complete",
        headers={"Authorization": "Bearer secret"},
        json={
            "AccessionNumber": "VALID001",
            "CompleteReason": "operator_verified_completed",
        },
    )

    assert response.status_code == 200
    assert completed == [
        {
            "AccessionNumber": "VALID001",
            "CompleteReason": "operator_verified_completed",
        }
    ]


def test_inactive_row_can_be_admin_marked_completed() -> None:
    completed = []
    app = create_app(
        {
            "GATEWAY_API_TOKEN": "secret",
            "FETCH_WORKLIST": lambda: [valid_entry(Active=False)],
            "COMPLETE_WORKLIST_ENTRY": lambda payload: completed.append(payload) or {"updated": 1},
        }
    )
    client = app.test_client()

    response = client.post(
        "/admin/worklist/complete",
        headers={"Authorization": "Bearer secret"},
        json={
            "AccessionNumber": "VALID001",
            "CompleteReason": "gateway_match_missed",
        },
    )

    assert response.status_code == 200
    assert completed[0]["CompleteReason"] == "gateway_match_missed"


def test_cancelled_row_cannot_be_marked_completed() -> None:
    app = create_app(
        {
            "GATEWAY_API_TOKEN": "secret",
            "FETCH_WORKLIST": lambda: [valid_entry(Active=False, CancelledAt="2026-07-08T10:00:00+09:00")],
        }
    )
    client = app.test_client()

    response = client.post(
        "/admin/worklist/complete",
        headers={"Authorization": "Bearer secret"},
        json={
            "AccessionNumber": "VALID001",
            "CompleteReason": "operator_verified_completed",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["state"] == "cancelled"


def test_expired_row_cannot_be_marked_completed() -> None:
    app = create_app(
        {
            "GATEWAY_API_TOKEN": "secret",
            "FETCH_WORKLIST": lambda: [valid_entry(ExpiresAt="2000-01-01T00:00:00+00:00")],
        }
    )
    client = app.test_client()

    response = client.post(
        "/admin/worklist/complete",
        headers={"Authorization": "Bearer secret"},
        json={
            "AccessionNumber": "VALID001",
            "CompleteReason": "operator_verified_completed",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["state"] == "expired"


def test_completed_row_returns_conflict() -> None:
    app = create_app(
        {
            "GATEWAY_API_TOKEN": "secret",
            "FETCH_WORKLIST": lambda: [valid_entry(Active=False, CompletedAt="2026-07-08T10:00:00+09:00")],
        }
    )
    client = app.test_client()

    response = client.post(
        "/admin/worklist/complete",
        headers={"Authorization": "Bearer secret"},
        json={
            "AccessionNumber": "VALID001",
            "CompleteReason": "operator_verified_completed",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["state"] == "completed"


def test_completed_entry_appears_under_completed_and_all_after_admin_complete(tmp_path) -> None:
    path = write_worklist(tmp_path, [valid_entry()])
    audit_db = tmp_path / "audit.sqlite3"
    server = start_test_api(path, audit_db)
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    app = create_app(
        {
            "GATEWAY_API_TOKEN": "secret",
            "MWL_API_BASE_URL": base_url,
        }
    )
    client = app.test_client()
    try:
        response = client.post(
            "/admin/worklist/complete",
            headers={"Authorization": "Bearer secret"},
            json={
                "AccessionNumber": "VALID001",
                "CompleteReason": "operator_verified_completed",
            },
        )
        entries = client.get("/imaging/worklist").get_json()["entries"]
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 200
    assert entries[0]["state"] == "completed"
    assert entries[0]["CompletedAt"]
    rows = audit_rows(audit_db)
    assert rows[0]["status"] == "completed"


def test_gateway_logs_do_not_contain_patient_demographics(caplog) -> None:
    app = create_app(
        {
            "GATEWAY_API_TOKEN": "secret",
            "FETCH_WORKLIST": lambda: [
                valid_entry(
                    PatientName="홍길동",
                    PatientBirthDate="19800101",
                    PatientSex="M",
                    PatientID="CHART-001",
                )
            ],
            "COMPLETE_WORKLIST_ENTRY": lambda payload: {"updated": 1},
        }
    )
    client = app.test_client()

    response = client.post(
        "/admin/worklist/complete",
        headers={"Authorization": "Bearer secret"},
        json={
            "AccessionNumber": "VALID001",
            "CompleteReason": "operator_verified_completed",
        },
    )

    assert response.status_code == 200
    assert "홍길동" not in caplog.text
    assert "19800101" not in caplog.text
    assert "CHART-001" not in caplog.text
