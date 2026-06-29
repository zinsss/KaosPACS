from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("kaospacs.gateway.mwl")


@dataclass(frozen=True)
class MwlResponse:
    status_code: int
    payload: Any


class MwlHttpError(Exception):
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"MWL API returned status {status_code}")


class MwlUnavailableError(Exception):
    pass


class MwlApiClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_worklist(self) -> MwlResponse:
        return self._request("GET", "/worklist")

    def put_worklist(self, payload: dict[str, Any]) -> MwlResponse:
        return self._request("PUT", "/worklist", payload)

    def complete_worklist(self, payload: dict[str, Any]) -> MwlResponse:
        return self._request("POST", "/worklist/complete", payload)

    def cancel_worklist(self, payload: dict[str, Any]) -> MwlResponse:
        return self._request("POST", "/worklist/cancel", payload)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> MwlResponse:
        url = f"{self.base_url}{path}"
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = Request(url, data=body, headers=headers, method=method)
        LOGGER.info("MWL API request method=%s path=%s", method, path)

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = _read_json_response(response.read())
                status_code = response.status
                LOGGER.info(
                    "MWL API response method=%s path=%s status=%s",
                    method,
                    path,
                    status_code,
                )
                return MwlResponse(status_code=status_code, payload=response_payload)
        except HTTPError as error:
            response_payload = _read_json_response(error.read())
            LOGGER.warning(
                "MWL API returned error method=%s path=%s status=%s",
                method,
                path,
                error.code,
            )
            raise MwlHttpError(error.code, response_payload) from error
        except (TimeoutError, socket.timeout, URLError) as error:
            LOGGER.error("MWL API unavailable method=%s path=%s error=%s", method, path, error)
            raise MwlUnavailableError("MWL API unavailable") from error


def _read_json_response(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "MWL API returned a non-JSON response"}
