from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

def _load_web_create_app():
    module_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("kaospacs_web_app_main", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module.create_app, module


create_app, web_main_module = _load_web_create_app()


def sample_entries():
    return [
        {
            "state": "active",
            "AccessionNumber": "ACC-1",
            "PatientID": "P-1",
            "PatientName": "홍길동",
            "PatientBirthDate": "19800101",
            "PatientSex": "M",
            "Modality": "BMD",
            "ScheduledAt": "2026-07-08T09:00:00",
            "Description": "골밀도 검사",
        },
        {
            "state": "completed",
            "AccessionNumber": "ACC-2",
            "PatientID": "P-2",
            "PatientName": "Alice",
            "PatientBirthDate": "19900101",
            "PatientSex": "F",
            "Modality": "CR",
            "ScheduledAt": "2026-07-08T10:00:00",
            "Description": "Chest",
        },
    ]


def test_imaging_worklist_filters_active_and_completed() -> None:
    app = create_app({"FETCH_IMAGING_WORKLIST": sample_entries})
    client = app.test_client()

    active = client.get("/imaging/worklist?filter=active")
    completed = client.get("/imaging/worklist?filter=completed")
    all_rows = client.get("/imaging/worklist?filter=all")

    active_text = active.get_data(as_text=True)
    completed_text = completed.get_data(as_text=True)
    all_text = all_rows.get_data(as_text=True)

    assert "ACC-1" in active_text
    assert "ACC-2" not in active_text
    assert "ACC-2" in completed_text
    assert "ACC-1" in all_text and "ACC-2" in all_text


def test_mark_complete_button_only_shows_for_active_or_inactive() -> None:
    app = create_app({"FETCH_IMAGING_WORKLIST": sample_entries})
    client = app.test_client()

    response = client.get("/imaging/worklist?filter=all")
    text = response.get_data(as_text=True)

    assert text.count("Mark Complete") == 1


def test_web_calls_gateway_admin_endpoint_not_mwl_directly(monkeypatch) -> None:
    calls = []

    def fake_request_json(**kwargs):
        calls.append(kwargs)
        if kwargs["path"] == "/imaging/worklist":
            return {"entries": sample_entries()}
        return {"updated": 1}

    monkeypatch.setattr(web_main_module, "_request_json", fake_request_json)
    app = create_app(
        {
            "GATEWAY_BASE_URL": "http://gateway:8060",
            "GATEWAY_API_TOKEN": "token-123",
        }
    )
    client = app.test_client()

    response = client.post(
        "/imaging/worklist/mark-complete",
        data={
            "AccessionNumber": "ACC-1",
            "CompleteReason": "operator_verified_completed",
        },
    )

    assert response.status_code == 302
    assert any(call["path"] == "/admin/worklist/complete" for call in calls)
    assert not any("8055" in call["base_url"] for call in calls)


def test_gateway_unavailable_shows_safe_message() -> None:
    app = create_app({"FETCH_IMAGING_WORKLIST": lambda: (_ for _ in ()).throw(RuntimeError("down"))})
    client = app.test_client()

    response = client.get("/imaging/worklist")

    assert "KaosPACS Gateway unavailable" in response.get_data(as_text=True)
