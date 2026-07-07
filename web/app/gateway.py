from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("kaospacs.web.gateway")


@dataclass(frozen=True)
class OperationalMetadata:
    display_modality: str
    dicom_modality_original: str
    workflow_modality: str
    station_aet: str
    study_type: str
    aio_domain_candidate: str


class GatewayMetadataClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout_seconds: float = 3.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def get_by_study(self, orthanc_study_id: str) -> OperationalMetadata | None:
        if not orthanc_study_id:
            return None
        return self._get(
            f"/imaging/operational-metadata/study/{quote(orthanc_study_id, safe='')}"
        )

    def get_by_accession(self, accession_number: str) -> OperationalMetadata | None:
        if not accession_number:
            return None
        return self._get(
            f"/imaging/operational-metadata/accession/{quote(accession_number, safe='')}"
        )

    def _get(self, path: str) -> OperationalMetadata | None:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            f"{self.base_url}{path}",
            headers=headers,
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code != 404:
                LOGGER.warning(
                    "Gateway operational metadata lookup failed status=%s",
                    error.code,
                )
            return None
        except (TimeoutError, socket.timeout, URLError, OSError, json.JSONDecodeError) as error:
            LOGGER.warning(
                "Gateway operational metadata lookup unavailable exception=%s",
                error.__class__.__name__,
            )
            return None
        return _metadata_from_payload(payload)


def _metadata_from_payload(payload: Any) -> OperationalMetadata | None:
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    display_modality = _text(metadata.get("display_modality"))
    if not display_modality or display_modality == "Unknown":
        return None
    return OperationalMetadata(
        display_modality=display_modality,
        dicom_modality_original=_text(metadata.get("dicom_modality_original")),
        workflow_modality=_text(metadata.get("workflow_modality")),
        station_aet=_text(metadata.get("station_aet")),
        study_type=_text(metadata.get("study_type")),
        aio_domain_candidate=_text(metadata.get("aio_domain_candidate")),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
