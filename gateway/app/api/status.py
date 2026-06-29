from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.clients.orthanc import OrthancHttpClient
from app.config import GatewayConfig
from app.dicom.queue import get_queue_counts
from app.services.auth import is_auth_enabled


STATUS_VERSION = "0.1"
HTTP_STATUS_TIMEOUT_SECONDS = 1.0


def status_payload(config: GatewayConfig) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "gateway",
        "version": STATUS_VERSION,
        "auth": {
            "enabled": is_auth_enabled(config.gateway_api_token),
        },
        "dependencies": {
            "mwl_api": _check_http_dependency(
                config.mwl_api_url,
                "/health",
                config.mwl_api_timeout_seconds,
            ),
            "orthanc_http": OrthancHttpClient(
                config.orthanc_url,
                min(config.orthanc_timeout_seconds, HTTP_STATUS_TIMEOUT_SECONDS),
            ).is_reachable(),
            "gateway_audit_db": _check_audit_db(config.gateway_audit_db),
        },
        "gateway_dicom": {
            "enabled": config.gateway_dicom_enabled,
            "aet": config.gateway_dicom_aet,
            "bind": config.gateway_dicom_bind,
            "port": config.gateway_dicom_port,
            "storage_dir": str(config.gateway_dicom_storage_dir),
            "queue_enabled": config.gateway_dicom_queue_enabled,
            "queue_db": _check_queue_db(config.gateway_queue_db),
            "queue_counts": _queue_counts(config.gateway_queue_db),
            "forward_enabled": config.gateway_dicom_forward_enabled,
            "forward_target": {
                "host": config.orthanc_dicom_host,
                "port": config.orthanc_dicom_port,
                "aet": config.orthanc_dicom_aet,
            },
            "mode": "skeleton-test-only",
        },
        "ownership": {
            "storage_scp": {
                "aet": "VIEWREX",
                "port": 104,
                "owner": "orthanc",
                "stage": "transitional",
            },
            "mwl_scp": {
                "aet": "VIEWREX_WL",
                "port": 105,
                "owner": "mwl",
                "stage": "current-final",
            },
            "gateway_http": {
                "host": "127.0.0.1",
                "port": config.http_port,
                "owner": "gateway",
            },
        },
    }


def _check_http_dependency(
    base_url: str,
    path: str,
    configured_timeout_seconds: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    timeout_seconds = min(configured_timeout_seconds, HTTP_STATUS_TIMEOUT_SECONDS)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return {
                "url": base_url.rstrip("/"),
                "reachable": True,
                "status_code": response.status,
            }
    except HTTPError as error:
        return {
            "url": base_url.rstrip("/"),
            "reachable": False,
            "status_code": error.code,
        }
    except (OSError, TimeoutError, URLError):
        return {
            "url": base_url.rstrip("/"),
            "reachable": False,
            "status_code": None,
        }


def _check_audit_db(path: Path) -> dict[str, Any]:
    try:
        with sqlite3.connect(f"file:{path}?mode=rw", uri=True) as connection:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        reachable = True
    except sqlite3.Error:
        reachable = False

    return {
        "path": str(path),
        "reachable": reachable,
    }


def _check_queue_db(path: Path) -> dict[str, Any]:
    try:
        get_queue_counts(path)
        reachable = True
    except sqlite3.Error:
        reachable = False

    return {
        "path": str(path),
        "reachable": reachable,
    }


def _queue_counts(path: Path) -> dict[str, int] | None:
    try:
        return get_queue_counts(path)
    except sqlite3.Error:
        return None
