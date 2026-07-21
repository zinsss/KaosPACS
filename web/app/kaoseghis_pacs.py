from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("kaospacs.web")


@dataclass(frozen=True)
class PatientContextResult:
    chart_no: str = ""
    patient_name: str = ""
    patient_birth_date: str = ""
    patient_sex: str = ""
    source: str = ""
    confidence: str = ""
    status: str = "no_result"

    @property
    def found(self) -> bool:
        return self.status == "ok"


class KaosEghisPacsClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def fetch_patient_context(self, chart_no: str) -> PatientContextResult:
        chart_no = str(chart_no or "").strip()
        if not chart_no or not self.base_url:
            return PatientContextResult(status="not_configured")

        url = (
            f"{self.base_url}/api/kaospacs/patient-context"
            f"?chart_no={quote(chart_no, safe='')}"
        )
        headers = {"Accept": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            status = _status_for_http_error(exc.code)
            LOGGER.info("Patient context fallback result=%s", status)
            return PatientContextResult(status=status)
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            LOGGER.info(
                "Patient context fallback result=unavailable exception=%s",
                exc.__class__.__name__,
            )
            return PatientContextResult(status="unavailable")
        except Exception as exc:
            LOGGER.info(
                "Patient context fallback result=unavailable exception=%s",
                exc.__class__.__name__,
            )
            return PatientContextResult(status="unavailable")

        if not isinstance(payload, dict):
            return PatientContextResult(status="unavailable")

        LOGGER.info("Patient context fallback result=success")
        return _result_from_payload(payload)


def _result_from_payload(payload: dict[str, Any]) -> PatientContextResult:
    return PatientContextResult(
        chart_no=str(payload.get("chart_no") or "").strip(),
        patient_name=str(payload.get("patient_name") or "").strip(),
        patient_birth_date=str(payload.get("patient_birth_date") or "").strip(),
        patient_sex=str(payload.get("patient_sex") or "").strip(),
        source=str(payload.get("source") or "").strip(),
        confidence=str(payload.get("confidence") or "").strip(),
        status="ok",
    )


def _status_for_http_error(code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        404: "not_found",
        409: "ambiguous",
        503: "unavailable",
    }.get(code, "unavailable")
