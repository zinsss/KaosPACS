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
DEFAULT_ORTHANC_TIMEOUT_SECONDS = 3.0
DEFAULT_GATEWAY_AUDIT_DB = Path("/app/data/gateway_audit.sqlite3")
DEFAULT_GATEWAY_QUEUE_DB = Path("/app/data/gateway_queue.sqlite3")
DEFAULT_GATEWAY_DICOM_ENABLED = True
DEFAULT_GATEWAY_DICOM_AET = "VIEWREX"
DEFAULT_GATEWAY_DICOM_PORT = 104
DEFAULT_GATEWAY_DICOM_BIND = "0.0.0.0"
DEFAULT_GATEWAY_DICOM_STORAGE_DIR = Path("/app/data/dicom-inbox")
DEFAULT_GATEWAY_DICOM_QUEUE_ENABLED = False
DEFAULT_GATEWAY_QUEUE_WORKER_ENABLED = False
DEFAULT_GATEWAY_QUEUE_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_GATEWAY_QUEUE_MAX_ATTEMPTS = 10
DEFAULT_GATEWAY_DICOM_FORWARD_MODE = "direct"
DEFAULT_GATEWAY_DICOM_FORWARD_ENABLED = True
DEFAULT_ORTHANC_DICOM_HOST = "orthanc"
DEFAULT_ORTHANC_DICOM_PORT = 11112
DEFAULT_ORTHANC_DICOM_AET = "VIEWREX"
DEFAULT_GATEWAY_FORWARDING_AET = "KAOSPACS_GW"
DEFAULT_GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS = 10.0
DEFAULT_GATEWAY_DICOM_INSPECTION_ENABLED = True
DEFAULT_GATEWAY_DICOM_INSPECTION_REPORT_PATH = Path(
    "/app/data/dicom_inspection.jsonl"
)
DEFAULT_GATEWAY_DICOM_CHARSET_FIX_ENABLED = False
DEFAULT_GATEWAY_DICOM_CHARSET_FIX_MODE = "off"
DEFAULT_GATEWAY_DICOM_CHARSET_FIX_REPORT_PATH = Path(
    "/app/data/dicom_charset_fix.jsonl"
)


@dataclass(frozen=True)
class GatewayConfig:
    orthanc_url: str = DEFAULT_ORTHANC_URL
    mwl_api_url: str = DEFAULT_MWL_API_URL
    log_level: str = DEFAULT_LOG_LEVEL
    tz: str = DEFAULT_TZ
    http_host: str = DEFAULT_HTTP_HOST
    http_port: int = DEFAULT_HTTP_PORT
    mwl_api_timeout_seconds: float = DEFAULT_MWL_API_TIMEOUT_SECONDS
    orthanc_timeout_seconds: float = DEFAULT_ORTHANC_TIMEOUT_SECONDS
    gateway_audit_db: Path = DEFAULT_GATEWAY_AUDIT_DB
    gateway_queue_db: Path = DEFAULT_GATEWAY_QUEUE_DB
    gateway_api_token: str | None = None
    gateway_dicom_enabled: bool = DEFAULT_GATEWAY_DICOM_ENABLED
    gateway_dicom_aet: str = DEFAULT_GATEWAY_DICOM_AET
    gateway_dicom_port: int = DEFAULT_GATEWAY_DICOM_PORT
    gateway_dicom_bind: str = DEFAULT_GATEWAY_DICOM_BIND
    gateway_dicom_storage_dir: Path = DEFAULT_GATEWAY_DICOM_STORAGE_DIR
    gateway_dicom_queue_enabled: bool = DEFAULT_GATEWAY_DICOM_QUEUE_ENABLED
    gateway_queue_worker_enabled: bool = DEFAULT_GATEWAY_QUEUE_WORKER_ENABLED
    gateway_queue_poll_interval_seconds: float = (
        DEFAULT_GATEWAY_QUEUE_POLL_INTERVAL_SECONDS
    )
    gateway_queue_max_attempts: int = DEFAULT_GATEWAY_QUEUE_MAX_ATTEMPTS
    gateway_dicom_forward_mode: str = DEFAULT_GATEWAY_DICOM_FORWARD_MODE
    gateway_dicom_forward_enabled: bool = DEFAULT_GATEWAY_DICOM_FORWARD_ENABLED
    orthanc_dicom_host: str = DEFAULT_ORTHANC_DICOM_HOST
    orthanc_dicom_port: int = DEFAULT_ORTHANC_DICOM_PORT
    orthanc_dicom_aet: str = DEFAULT_ORTHANC_DICOM_AET
    gateway_forwarding_aet: str = DEFAULT_GATEWAY_FORWARDING_AET
    gateway_dicom_forward_timeout_seconds: float = (
        DEFAULT_GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS
    )
    gateway_dicom_inspection_enabled: bool = (
        DEFAULT_GATEWAY_DICOM_INSPECTION_ENABLED
    )
    gateway_dicom_inspection_report_path: Path = (
        DEFAULT_GATEWAY_DICOM_INSPECTION_REPORT_PATH
    )
    gateway_dicom_charset_fix_enabled: bool = (
        DEFAULT_GATEWAY_DICOM_CHARSET_FIX_ENABLED
    )
    gateway_dicom_charset_fix_mode: str = DEFAULT_GATEWAY_DICOM_CHARSET_FIX_MODE
    gateway_dicom_charset_fix_report_path: Path = (
        DEFAULT_GATEWAY_DICOM_CHARSET_FIX_REPORT_PATH
    )

    def safe_log_dict(self) -> dict[str, object]:
        values = asdict(self)
        values.pop("gateway_api_token", None)
        values["gateway_api_token_configured"] = self.gateway_api_token is not None
        return values


def _int_from_env(raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float_from_env(raw: str | None, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _bool_from_env(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {raw!r}")


def _forward_mode_from_env(raw: str | None) -> str:
    mode = (raw or DEFAULT_GATEWAY_DICOM_FORWARD_MODE).strip().lower()
    if mode not in {"direct", "queue"}:
        raise ValueError(
            "GATEWAY_DICOM_FORWARD_MODE must be one of: direct, queue"
        )
    return mode


def _charset_fix_mode_from_env(raw: str | None) -> str:
    mode = (raw or DEFAULT_GATEWAY_DICOM_CHARSET_FIX_MODE).strip().lower()
    if mode not in {"off", "iso_ir_149_to_utf8"}:
        raise ValueError(
            "GATEWAY_DICOM_CHARSET_FIX_MODE must be one of: off, iso_ir_149_to_utf8"
        )
    return mode


def _validate_config(config: GatewayConfig) -> GatewayConfig:
    if config.gateway_dicom_forward_mode == "queue":
        if not config.gateway_dicom_queue_enabled:
            raise ValueError(
                "GATEWAY_DICOM_FORWARD_MODE=queue requires "
                "GATEWAY_DICOM_QUEUE_ENABLED=true"
            )
        if not config.gateway_queue_worker_enabled:
            raise ValueError(
                "GATEWAY_DICOM_FORWARD_MODE=queue requires "
                "GATEWAY_QUEUE_WORKER_ENABLED=true"
            )
    return config


def load_config(env: Mapping[str, str] | None = None) -> GatewayConfig:
    source = environ if env is None else env
    raw_gateway_api_token = source.get("GATEWAY_API_TOKEN")
    gateway_api_token = raw_gateway_api_token if raw_gateway_api_token else None
    config = GatewayConfig(
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
        orthanc_timeout_seconds=_float_from_env(
            source.get("ORTHANC_TIMEOUT_SECONDS"),
            DEFAULT_ORTHANC_TIMEOUT_SECONDS,
        ),
        gateway_audit_db=Path(source.get("GATEWAY_AUDIT_DB", str(DEFAULT_GATEWAY_AUDIT_DB))),
        gateway_queue_db=Path(source.get("GATEWAY_QUEUE_DB", str(DEFAULT_GATEWAY_QUEUE_DB))),
        gateway_api_token=gateway_api_token,
        gateway_dicom_enabled=_bool_from_env(
            source.get("GATEWAY_DICOM_ENABLED"),
            DEFAULT_GATEWAY_DICOM_ENABLED,
        ),
        gateway_dicom_aet=source.get("GATEWAY_DICOM_AET", DEFAULT_GATEWAY_DICOM_AET),
        gateway_dicom_port=_int_from_env(
            source.get("GATEWAY_DICOM_PORT"),
            DEFAULT_GATEWAY_DICOM_PORT,
        ),
        gateway_dicom_bind=source.get("GATEWAY_DICOM_BIND", DEFAULT_GATEWAY_DICOM_BIND),
        gateway_dicom_storage_dir=Path(
            source.get("GATEWAY_DICOM_STORAGE_DIR", str(DEFAULT_GATEWAY_DICOM_STORAGE_DIR))
        ),
        gateway_dicom_queue_enabled=_bool_from_env(
            source.get("GATEWAY_DICOM_QUEUE_ENABLED"),
            DEFAULT_GATEWAY_DICOM_QUEUE_ENABLED,
        ),
        gateway_queue_worker_enabled=_bool_from_env(
            source.get("GATEWAY_QUEUE_WORKER_ENABLED"),
            DEFAULT_GATEWAY_QUEUE_WORKER_ENABLED,
        ),
        gateway_queue_poll_interval_seconds=_float_from_env(
            source.get("GATEWAY_QUEUE_POLL_INTERVAL_SECONDS"),
            DEFAULT_GATEWAY_QUEUE_POLL_INTERVAL_SECONDS,
        ),
        gateway_queue_max_attempts=_int_from_env(
            source.get("GATEWAY_QUEUE_MAX_ATTEMPTS"),
            DEFAULT_GATEWAY_QUEUE_MAX_ATTEMPTS,
        ),
        gateway_dicom_forward_mode=_forward_mode_from_env(
            source.get("GATEWAY_DICOM_FORWARD_MODE")
        ),
        gateway_dicom_forward_enabled=_bool_from_env(
            source.get("GATEWAY_DICOM_FORWARD_ENABLED"),
            DEFAULT_GATEWAY_DICOM_FORWARD_ENABLED,
        ),
        orthanc_dicom_host=source.get("ORTHANC_DICOM_HOST", DEFAULT_ORTHANC_DICOM_HOST),
        orthanc_dicom_port=_int_from_env(
            source.get("ORTHANC_DICOM_PORT"),
            DEFAULT_ORTHANC_DICOM_PORT,
        ),
        orthanc_dicom_aet=source.get("ORTHANC_DICOM_AET", DEFAULT_ORTHANC_DICOM_AET),
        gateway_forwarding_aet=source.get(
            "GATEWAY_FORWARDING_AET",
            DEFAULT_GATEWAY_FORWARDING_AET,
        ),
        gateway_dicom_forward_timeout_seconds=_float_from_env(
            source.get("GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS"),
            DEFAULT_GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS,
        ),
        gateway_dicom_inspection_enabled=_bool_from_env(
            source.get("GATEWAY_DICOM_INSPECTION_ENABLED"),
            DEFAULT_GATEWAY_DICOM_INSPECTION_ENABLED,
        ),
        gateway_dicom_inspection_report_path=Path(
            source.get(
                "GATEWAY_DICOM_INSPECTION_REPORT_PATH",
                str(DEFAULT_GATEWAY_DICOM_INSPECTION_REPORT_PATH),
            )
        ),
        gateway_dicom_charset_fix_enabled=_bool_from_env(
            source.get("GATEWAY_DICOM_CHARSET_FIX_ENABLED"),
            DEFAULT_GATEWAY_DICOM_CHARSET_FIX_ENABLED,
        ),
        gateway_dicom_charset_fix_mode=_charset_fix_mode_from_env(
            source.get("GATEWAY_DICOM_CHARSET_FIX_MODE")
        ),
        gateway_dicom_charset_fix_report_path=Path(
            source.get(
                "GATEWAY_DICOM_CHARSET_FIX_REPORT_PATH",
                str(DEFAULT_GATEWAY_DICOM_CHARSET_FIX_REPORT_PATH),
            )
        ),
    )
    return _validate_config(config)
