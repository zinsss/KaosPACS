from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("kaospacs.gateway.orthanc")


@dataclass(frozen=True)
class OrthancResult:
    reachable: bool
    status_code: int | None
    payload: Any = None
    error: str | None = None


class OrthancHttpClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_system(self) -> OrthancResult:
        return self._request("GET", "/system")

    def find_study_id_by_uid(self, study_instance_uid: str) -> str | None:
        if not study_instance_uid:
            return None
        result = self._request(
            "POST",
            "/tools/find",
            {
                "Level": "Study",
                "Query": {
                    "StudyInstanceUID": study_instance_uid,
                },
            },
        )
        if not result.reachable or not isinstance(result.payload, list):
            return None
        if not result.payload:
            return None
        return str(result.payload[0])

    def is_reachable(self) -> dict[str, Any]:
        result = self.get_system()
        return {
            "url": self.base_url,
            "reachable": result.reachable,
            "status_code": result.status_code,
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> OrthancResult:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        LOGGER.info("Orthanc HTTP request method=%s path=%s", method, path)

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = _read_json_response(response.read())
                LOGGER.info(
                    "Orthanc HTTP response method=%s path=%s status=%s",
                    method,
                    path,
                    response.status,
                )
                return OrthancResult(
                    reachable=True,
                    status_code=response.status,
                    payload=payload,
                )
        except HTTPError as error:
            LOGGER.warning(
                "Orthanc HTTP returned error method=%s path=%s status=%s",
                method,
                path,
                error.code,
            )
            return OrthancResult(
                reachable=False,
                status_code=error.code,
                error="orthanc_http_error",
            )
        except (TimeoutError, socket.timeout, URLError, OSError) as error:
            LOGGER.warning(
                "Orthanc HTTP unavailable method=%s path=%s error=%s",
                method,
                path,
                error.__class__.__name__,
            )
            return OrthancResult(
                reachable=False,
                status_code=None,
                error="orthanc_unavailable",
            )


def _read_json_response(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "Orthanc returned a non-JSON response"}
