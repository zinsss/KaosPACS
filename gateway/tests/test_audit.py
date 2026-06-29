import sqlite3

from app.audit import AUDIT_TABLE, init_audit_db, record_gateway_event


FORBIDDEN_COLUMNS = {
    "PatientName",
    "PatientBirthDate",
    "PatientSex",
    "resident_id",
    "phone",
    "address",
    "diagnosis",
    "emr_note",
    "payload",
}


def table_columns(db_path):
    with sqlite3.connect(db_path) as connection:
        return {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({AUDIT_TABLE})")
        }


def test_audit_db_initializes(tmp_path) -> None:
    db_path = tmp_path / "gateway_audit.sqlite3"

    init_audit_db(db_path)

    assert {
        "id",
        "event_type",
        "request_path",
        "accession_number",
        "status_code",
        "success",
        "error_code",
        "created_at",
    } <= table_columns(db_path)


def test_audit_schema_does_not_contain_demographic_columns(tmp_path) -> None:
    db_path = tmp_path / "gateway_audit.sqlite3"

    init_audit_db(db_path)

    assert table_columns(db_path).isdisjoint(FORBIDDEN_COLUMNS)


def test_audit_records_minimal_event(tmp_path) -> None:
    db_path = tmp_path / "gateway_audit.sqlite3"

    record_gateway_event(
        db_path,
        event_type="worklist_complete",
        request_path="/worklist/complete",
        accession_number="ACC1",
        status_code=200,
        success=True,
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            f"""
            SELECT event_type, request_path, accession_number, status_code, success, error_code
            FROM {AUDIT_TABLE}
            """
        ).fetchone()

    assert row == ("worklist_complete", "/worklist/complete", "ACC1", 200, 1, None)
