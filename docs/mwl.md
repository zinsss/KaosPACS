# Modality Worklist

KaosPACS MWL is a small DICOM Modality Worklist SCP for legacy modality
testing. The first milestone is intentionally hardcoded and does not connect to
eGHIS.

## Fixed Production Identity

```text
AET:  VIEWREX_WL
Port: 105
```

Legacy modalities expect this identity. Production behavior must preserve it.

## Current Milestone

The current implementation reads worklist entries from JSON:

```text
mwl/config/worklist.json
```

This JSON file is the current integration boundary. KaosPACS remains
EMR-agnostic: it does not connect to eGHIS and contains no eGHIS database code.
Later, KaosEghis-PACS can update this JSON file or call a KaosPACS API that
updates the same worklist model.

The checked-in sample contains fictional test entries including:

```text
PatientName: TEST^BMD
PatientID: KAOSMWL001
AccessionNumber: KAOSMWL001
Modality: BMD
ScheduledStationAETitle: BMD
ScheduledProcedureStepDescription: BMD TEST
```

The service supports:

- Verification C-ECHO
- Modality Worklist C-FIND
- Multiple JSON worklist entries
- Query matching by PatientID, AccessionNumber, Modality, and
  ScheduledStationAETitle; blank query fields are wildcards
- C-FIND query, match, and completion logging

Run it with:

```bash
docker compose up -d mwl
docker logs --tail=100 kaospacs-mwl
```

Query it from inside the container:

```bash
docker compose exec mwl python tools/query_mwl.py
```

The utility defaults to `127.0.0.1:105` with calling AE `KAOSPACS_TEST` and
called AE `VIEWREX_WL`, then prints the returned patient and scheduled step
fields. Optional filters are available:

```bash
docker compose exec mwl python tools/query_mwl.py --patient-id KAOSMWL001
docker compose exec mwl python tools/query_mwl.py --accession-number KAOSMWL002
docker compose exec mwl python tools/query_mwl.py --modality BMD --station-aet BMD
```

## Later Milestone

Later MWL should derive worklist entries from eGHIS orders using read-only
database access or a read-only upstream feed. eGHIS polling must never mutate
the eGHIS database.

Do not add eGHIS polling, route logic, or Orthanc-side behavior in this
milestone.
