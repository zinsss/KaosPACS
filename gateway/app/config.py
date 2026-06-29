from __future__ import annotations

from dataclasses import asdict, dataclass
from os import environ
from pathlib import Path
from typing import Mapping


DEFAULT_ORTHANC_URL = "http://orthanc:8042"
DEFAULT_MWL_API_URL = "http://mwl:8055"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_TZ = "Asia/Seoul"
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8060
DEFAULT_MWL_API_TIMEOUT_SECONDS = 3.0
DEFAULT_GATEWAY_AUDIT_DB = Path("/app/data/gateway_audit.sqlite3")


@dataclass(frozen=True)
class GatewayConfig:
    orthanc_url: str = DEFAULT_ORTHANC_URL
    mwl_api_url: str = DEFAULT_MWL_API_URL
    log_level: str = DEFAULT_LOG_LEVEL
    tz: str = DEFAULT_TZ
    http_host: str = DEFAULT_HTTP_HOST
    http_port: int = DEFAULT_HTTP_PORT
    mwl_api_timeout_seconds: float = DEFAULT_MWL_API_TIMEOUT_SECONDS
    gateway_audit_db: Path = DEFAULT_GATEWAY_AUDIT_DB

    def safe_log_dict(self) -> dict[str, object]:
        return asdict(self)


def _int_from_env(raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float_from_env(raw: str | None, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def load_config(env: Mapping[str, str] | None = None) -> GatewayConfig:
    source = environ if env is None else env
    return GatewayConfig(
        orthanc_url=source.get("ORTHANC_URL", DEFAULT_ORTHANC_URL),
        mwl_api_url=source.get("MWL_API_URL", DEFAULT_MWL_API_URL),
        log_level=source.get("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
        tz=source.get("TZ", DEFAULT_TZ),
        http_host=source.get("GATEWAY_HTTP_HOST", DEFAULT_HTTP_HOST),
        http_port=_int_from_env(source.get("GATEWAY_HTTP_PORT"), DEFAULT_HTTP_PORT),
        mwl_api_timeout_seconds=_float_from_env(
            source.get("MWL_API_TIMEOUT_SECONDS"),
            DEFAULT_MWL_API_TIMEOUT_SECONDS,
        ),
        gateway_audit_db=Path(source.get("GATEWAY_AUDIT_DB", str(DEFAULT_GATEWAY_AUDIT_DB))),
    )
