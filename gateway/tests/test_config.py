import pytest

from app.config import (
    DEFAULT_GATEWAY_AUDIT_DB,
    DEFAULT_GATEWAY_QUEUE_DB,
    DEFAULT_GATEWAY_DICOM_AET,
    DEFAULT_GATEWAY_DICOM_BIND,
    DEFAULT_GATEWAY_DICOM_ENABLED,
    DEFAULT_GATEWAY_DICOM_FORWARD_ENABLED,
    DEFAULT_GATEWAY_DICOM_FORWARD_MODE,
    DEFAULT_GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS,
    DEFAULT_GATEWAY_DICOM_CHARSET_FIX_ENABLED,
    DEFAULT_GATEWAY_DICOM_CHARSET_FIX_MODE,
    DEFAULT_GATEWAY_DICOM_CHARSET_FIX_REPORT_PATH,
    DEFAULT_GATEWAY_DICOM_INSPECTION_ENABLED,
    DEFAULT_GATEWAY_DICOM_INSPECTION_REPORT_PATH,
    DEFAULT_GATEWAY_DICOM_QUEUE_ENABLED,
    DEFAULT_GATEWAY_DICOM_PORT,
    DEFAULT_GATEWAY_DICOM_STORAGE_DIR,
    DEFAULT_GATEWAY_FORWARDING_AET,
    DEFAULT_GATEWAY_QUEUE_MAX_ATTEMPTS,
    DEFAULT_GATEWAY_QUEUE_POLL_INTERVAL_SECONDS,
    DEFAULT_GATEWAY_QUEUE_WORKER_ENABLED,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MWL_API_TIMEOUT_SECONDS,
    DEFAULT_MWL_API_URL,
    DEFAULT_ORTHANC_DICOM_AET,
    DEFAULT_ORTHANC_DICOM_HOST,
    DEFAULT_ORTHANC_DICOM_PORT,
    DEFAULT_ORTHANC_TIMEOUT_SECONDS,
    DEFAULT_ORTHANC_URL,
    DEFAULT_TZ,
    load_config,
)


def test_config_defaults() -> None:
    config = load_config({})

    assert config.orthanc_url == DEFAULT_ORTHANC_URL
    assert config.mwl_api_url == DEFAULT_MWL_API_URL
    assert config.log_level == DEFAULT_LOG_LEVEL
    assert config.tz == DEFAULT_TZ
    assert config.http_port == 8060
    assert config.mwl_api_timeout_seconds == DEFAULT_MWL_API_TIMEOUT_SECONDS
    assert config.orthanc_timeout_seconds == DEFAULT_ORTHANC_TIMEOUT_SECONDS
    assert config.gateway_audit_db == DEFAULT_GATEWAY_AUDIT_DB
    assert config.gateway_queue_db == DEFAULT_GATEWAY_QUEUE_DB
    assert config.gateway_api_token is None
    assert config.gateway_dicom_enabled == DEFAULT_GATEWAY_DICOM_ENABLED
    assert config.gateway_dicom_aet == DEFAULT_GATEWAY_DICOM_AET
    assert config.gateway_dicom_port == DEFAULT_GATEWAY_DICOM_PORT
    assert config.gateway_dicom_bind == DEFAULT_GATEWAY_DICOM_BIND
    assert config.gateway_dicom_storage_dir == DEFAULT_GATEWAY_DICOM_STORAGE_DIR
    assert config.gateway_dicom_queue_enabled == DEFAULT_GATEWAY_DICOM_QUEUE_ENABLED
    assert config.gateway_queue_worker_enabled == DEFAULT_GATEWAY_QUEUE_WORKER_ENABLED
    assert (
        config.gateway_queue_poll_interval_seconds
        == DEFAULT_GATEWAY_QUEUE_POLL_INTERVAL_SECONDS
    )
    assert config.gateway_queue_max_attempts == DEFAULT_GATEWAY_QUEUE_MAX_ATTEMPTS
    assert config.gateway_dicom_forward_mode == DEFAULT_GATEWAY_DICOM_FORWARD_MODE
    assert config.gateway_dicom_forward_enabled == DEFAULT_GATEWAY_DICOM_FORWARD_ENABLED
    assert config.orthanc_dicom_host == DEFAULT_ORTHANC_DICOM_HOST
    assert config.orthanc_dicom_port == DEFAULT_ORTHANC_DICOM_PORT
    assert config.orthanc_dicom_aet == DEFAULT_ORTHANC_DICOM_AET
    assert config.gateway_forwarding_aet == DEFAULT_GATEWAY_FORWARDING_AET
    assert (
        config.gateway_dicom_forward_timeout_seconds
        == DEFAULT_GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS
    )
    assert (
        config.gateway_dicom_inspection_enabled
        == DEFAULT_GATEWAY_DICOM_INSPECTION_ENABLED
    )
    assert (
        config.gateway_dicom_inspection_report_path
        == DEFAULT_GATEWAY_DICOM_INSPECTION_REPORT_PATH
    )
    assert (
        config.gateway_dicom_charset_fix_enabled
        == DEFAULT_GATEWAY_DICOM_CHARSET_FIX_ENABLED
    )
    assert config.gateway_dicom_charset_fix_mode == DEFAULT_GATEWAY_DICOM_CHARSET_FIX_MODE
    assert (
        config.gateway_dicom_charset_fix_report_path
        == DEFAULT_GATEWAY_DICOM_CHARSET_FIX_REPORT_PATH
    )
    assert "gateway_api_token" not in config.safe_log_dict()
    assert config.safe_log_dict()["gateway_api_token_configured"] is False


def test_config_env_overrides() -> None:
    config = load_config(
        {
            "ORTHANC_URL": "http://orthanc.local:8042",
            "MWL_API_URL": "http://mwl.local:8055",
            "LOG_LEVEL": "debug",
            "TZ": "UTC",
            "GATEWAY_HTTP_PORT": "18060",
            "MWL_API_TIMEOUT_SECONDS": "7.5",
            "ORTHANC_TIMEOUT_SECONDS": "4.5",
            "GATEWAY_AUDIT_DB": "/tmp/gateway-audit.sqlite3",
            "GATEWAY_QUEUE_DB": "/tmp/gateway-queue.sqlite3",
            "GATEWAY_API_TOKEN": "secret-token",
            "GATEWAY_DICOM_ENABLED": "true",
            "GATEWAY_DICOM_AET": "GW_TEST",
            "GATEWAY_DICOM_PORT": "11105",
            "GATEWAY_DICOM_BIND": "127.0.0.2",
            "GATEWAY_DICOM_STORAGE_DIR": "/tmp/dicom-inbox",
            "GATEWAY_DICOM_QUEUE_ENABLED": "true",
            "GATEWAY_QUEUE_WORKER_ENABLED": "true",
            "GATEWAY_QUEUE_POLL_INTERVAL_SECONDS": "1.5",
            "GATEWAY_QUEUE_MAX_ATTEMPTS": "3",
            "GATEWAY_DICOM_FORWARD_MODE": "queue",
            "GATEWAY_DICOM_FORWARD_ENABLED": "true",
            "ORTHANC_DICOM_HOST": "orthanc.local",
            "ORTHANC_DICOM_PORT": "4242",
            "ORTHANC_DICOM_AET": "ORTHANC_TEST",
            "GATEWAY_FORWARDING_AET": "GW_FORWARD",
            "GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS": "12.5",
            "GATEWAY_DICOM_INSPECTION_ENABLED": "false",
            "GATEWAY_DICOM_INSPECTION_REPORT_PATH": "/tmp/inspection.jsonl",
            "GATEWAY_DICOM_CHARSET_FIX_ENABLED": "true",
            "GATEWAY_DICOM_CHARSET_FIX_MODE": "iso_ir_149_to_utf8",
            "GATEWAY_DICOM_CHARSET_FIX_REPORT_PATH": "/tmp/charset-fix.jsonl",
        }
    )

    assert config.orthanc_url == "http://orthanc.local:8042"
    assert config.mwl_api_url == "http://mwl.local:8055"
    assert config.log_level == "DEBUG"
    assert config.tz == "UTC"
    assert config.http_port == 18060
    assert config.mwl_api_timeout_seconds == 7.5
    assert config.orthanc_timeout_seconds == 4.5
    assert str(config.gateway_audit_db) == "/tmp/gateway-audit.sqlite3"
    assert str(config.gateway_queue_db) == "/tmp/gateway-queue.sqlite3"
    assert config.gateway_api_token == "secret-token"
    assert config.gateway_dicom_enabled is True
    assert config.gateway_dicom_aet == "GW_TEST"
    assert config.gateway_dicom_port == 11105
    assert config.gateway_dicom_bind == "127.0.0.2"
    assert str(config.gateway_dicom_storage_dir) == "/tmp/dicom-inbox"
    assert config.gateway_dicom_queue_enabled is True
    assert config.gateway_queue_worker_enabled is True
    assert config.gateway_queue_poll_interval_seconds == 1.5
    assert config.gateway_queue_max_attempts == 3
    assert config.gateway_dicom_forward_mode == "queue"
    assert config.gateway_dicom_forward_enabled is True
    assert config.orthanc_dicom_host == "orthanc.local"
    assert config.orthanc_dicom_port == 4242
    assert config.orthanc_dicom_aet == "ORTHANC_TEST"
    assert config.gateway_forwarding_aet == "GW_FORWARD"
    assert config.gateway_dicom_forward_timeout_seconds == 12.5
    assert config.gateway_dicom_inspection_enabled is False
    assert str(config.gateway_dicom_inspection_report_path) == "/tmp/inspection.jsonl"
    assert config.gateway_dicom_charset_fix_enabled is True
    assert config.gateway_dicom_charset_fix_mode == "iso_ir_149_to_utf8"
    assert str(config.gateway_dicom_charset_fix_report_path) == "/tmp/charset-fix.jsonl"
    assert "secret-token" not in str(config.safe_log_dict())
    assert config.safe_log_dict()["gateway_api_token_configured"] is True


def test_empty_gateway_api_token_disables_auth() -> None:
    config = load_config({"GATEWAY_API_TOKEN": ""})

    assert config.gateway_api_token is None
    assert config.safe_log_dict()["gateway_api_token_configured"] is False


def test_unknown_forward_mode_fails_config_validation() -> None:
    with pytest.raises(ValueError, match="GATEWAY_DICOM_FORWARD_MODE"):
        load_config({"GATEWAY_DICOM_FORWARD_MODE": "mystery"})


def test_unknown_charset_fix_mode_fails_config_validation() -> None:
    with pytest.raises(ValueError, match="GATEWAY_DICOM_CHARSET_FIX_MODE"):
        load_config({"GATEWAY_DICOM_CHARSET_FIX_MODE": "guess_everything"})


def test_queue_forward_mode_requires_queue_enabled() -> None:
    with pytest.raises(ValueError, match="GATEWAY_DICOM_QUEUE_ENABLED"):
        load_config(
            {
                "GATEWAY_DICOM_FORWARD_MODE": "queue",
                "GATEWAY_DICOM_QUEUE_ENABLED": "false",
                "GATEWAY_QUEUE_WORKER_ENABLED": "true",
            }
        )


def test_queue_forward_mode_requires_worker_enabled() -> None:
    with pytest.raises(ValueError, match="GATEWAY_QUEUE_WORKER_ENABLED"):
        load_config(
            {
                "GATEWAY_DICOM_FORWARD_MODE": "queue",
                "GATEWAY_DICOM_QUEUE_ENABLED": "true",
                "GATEWAY_QUEUE_WORKER_ENABLED": "false",
            }
        )
