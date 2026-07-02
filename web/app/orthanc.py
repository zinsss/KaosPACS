from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class StudySummary:
    orthanc_id: str
    study_instance_uid: str
    accession_number: str
    patient_id: str
    patient_name: str
    patient_birth_date: str
    patient_sex: str
    study_date: str
    study_time: str
    study_description: str
    modalities: list[str]
    series_count: int
    instance_count: int
    thumbnail_instance_id: str


class OrthancClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> bool:
        try:
            self._json("/system")
        except Exception:
            return False
        return True

    def studies(self, *, query: str = "", limit: int = 100) -> list[StudySummary]:
        payload = self._json("/studies", {"expand": "true"})
        studies = [self._study_payload(item) for item in payload]
        summaries = [self._summary(item) for item in studies]
        summaries = [item for item in summaries if self._matches_query(item, query)]
        summaries.sort(
            key=lambda item: (
                item.study_date or "",
                item.study_time or "",
                item.accession_number or "",
            ),
            reverse=True,
        )
        return summaries[:limit]

    def preview(self, instance_id: str) -> tuple[bytes, str]:
        url = f"{self.base_url}/instances/{instance_id}/preview"
        request = Request(url, headers={"Accept": "image/png,image/jpeg,*/*"})
        with urlopen(request, timeout=self.timeout) as response:
            content_type = response.headers.get("Content-Type", "image/png")
            return response.read(), content_type

    def _summary(self, study: dict[str, Any]) -> StudySummary:
        study_id = str(study.get("ID", ""))
        study_tags = study.get("MainDicomTags") or {}
        patient_tags = study.get("PatientMainDicomTags") or {}
        series_ids = [str(item) for item in (study.get("Series") or [])]

        modalities: set[str] = set()
        instance_count = 0
        thumbnail_instance_id = ""
        for series_id in series_ids:
            series = self._series_payload(series_id)
            series_tags = series.get("MainDicomTags") or {}
            if series_tags.get("Modality"):
                modalities.add(str(series_tags["Modality"]))
            instances = [str(item) for item in (series.get("Instances") or [])]
            instance_count += len(instances)
            if not thumbnail_instance_id and instances:
                thumbnail_instance_id = instances[0]

        return StudySummary(
            orthanc_id=study_id,
            study_instance_uid=str(study_tags.get("StudyInstanceUID", "")),
            accession_number=str(study_tags.get("AccessionNumber", "")),
            patient_id=str(patient_tags.get("PatientID", "")),
            patient_name=str(patient_tags.get("PatientName", "")),
            patient_birth_date=str(patient_tags.get("PatientBirthDate", "")),
            patient_sex=str(patient_tags.get("PatientSex", "")),
            study_date=str(study_tags.get("StudyDate", "")),
            study_time=str(study_tags.get("StudyTime", "")),
            study_description=str(study_tags.get("StudyDescription", "")),
            modalities=sorted(modalities),
            series_count=len(series_ids),
            instance_count=instance_count,
            thumbnail_instance_id=thumbnail_instance_id,
        )

    def _study_payload(self, item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return item
        return self._json(f"/studies/{item}")

    def _series_payload(self, series_id: str) -> dict[str, Any]:
        return self._json(f"/series/{series_id}")

    def _json(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        with urlopen(f"{self.base_url}{path}{query}", timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _matches_query(study: StudySummary, query: str) -> bool:
        needle = query.strip().lower()
        if not needle:
            return True
        haystack = " ".join(
            [
                study.accession_number,
                study.patient_id,
                study.patient_name,
                study.study_description,
                study.study_instance_uid,
                " ".join(study.modalities),
            ]
        ).lower()
        return needle in haystack
