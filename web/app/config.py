from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    http_host: str
    http_port: int
    orthanc_url: str
    orthanc_public_url: str
    weasis_dicomweb_url: str
    kaospacs_aio_url: str
    gateway_url: str
    gateway_api_token: str
    study_limit: int
    upload_max_bytes: int
    auth_username: str
    auth_password: str
    admin_auth_required: bool


def load_config() -> Config:
    orthanc_public_url = _strip_slash(
        os.getenv("WEB_ORTHANC_PUBLIC_URL", "http://192.168.0.200:8042")
    )
    return Config(
        http_host=os.getenv("WEB_HTTP_HOST", "0.0.0.0"),
        http_port=_int_env("WEB_HTTP_PORT", 8070),
        orthanc_url=_strip_slash(os.getenv("WEB_ORTHANC_URL", "http://orthanc:8042")),
        orthanc_public_url=orthanc_public_url,
        weasis_dicomweb_url=_strip_slash(
            os.getenv("WEASIS_DICOMWEB_URL", f"{orthanc_public_url}/dicom-web")
        ),
        kaospacs_aio_url=_strip_slash(
            os.getenv("KAOSPACS_AIO_URL", "http://127.0.0.1:8056")
        ),
        gateway_url=_strip_slash(os.getenv("WEB_GATEWAY_URL", "http://gateway:8060")),
        gateway_api_token=os.getenv("WEB_GATEWAY_API_TOKEN")
        or os.getenv("GATEWAY_API_TOKEN", ""),
        study_limit=_int_env("WEB_STUDY_LIMIT", 100),
        upload_max_bytes=_int_env("WEB_UPLOAD_MAX_BYTES", 25 * 1024 * 1024),
        auth_username=os.getenv("WEB_AUTH_USERNAME", "kaospacs"),
        auth_password=os.getenv("WEB_AUTH_PASSWORD", ""),
        admin_auth_required=_bool_env("WEB_ADMIN_AUTH_REQUIRED", False),
    )


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return int(raw)


def _strip_slash(value: str) -> str:
    return value.rstrip("/")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
