from app.config import (
    DEFAULT_GATEWAY_AUDIT_DB,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MWL_API_TIMEOUT_SECONDS,
    DEFAULT_MWL_API_URL,
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
    assert config.gateway_audit_db == DEFAULT_GATEWAY_AUDIT_DB
    assert config.gateway_api_token is None
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
            "GATEWAY_AUDIT_DB": "/tmp/gateway-audit.sqlite3",
            "GATEWAY_API_TOKEN": "secret-token",
        }
    )

    assert config.orthanc_url == "http://orthanc.local:8042"
    assert config.mwl_api_url == "http://mwl.local:8055"
    assert config.log_level == "DEBUG"
    assert config.tz == "UTC"
    assert config.http_port == 18060
    assert config.mwl_api_timeout_seconds == 7.5
    assert str(config.gateway_audit_db) == "/tmp/gateway-audit.sqlite3"
    assert config.gateway_api_token == "secret-token"
    assert "secret-token" not in str(config.safe_log_dict())
    assert config.safe_log_dict()["gateway_api_token_configured"] is True


def test_empty_gateway_api_token_disables_auth() -> None:
    config = load_config({"GATEWAY_API_TOKEN": ""})

    assert config.gateway_api_token is None
    assert config.safe_log_dict()["gateway_api_token_configured"] is False
