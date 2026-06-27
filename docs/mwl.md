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

This JSON file and the local MWL HTTP API are the current integration boundary.
KaosPACS remains EMR-agnostic: it does not connect to eGHIS and contains no
eGHIS database code. Later, KaosEghis-PACS can update this JSON file or call the
KaosPACS MWL API that updates the same worklist model.

The checked-in sample contains fictional test entries including:

```text
Active: true
ExpiresAt: 2099-12-31T23:59:59+09:00
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
- Optional `Active` flags; inactive entries are not returned
- Optional `ExpiresAt` ISO datetime values; expired entries are not returned
- Per-entry validation with warning logs for invalid entries
- Query matching by PatientID, AccessionNumber, Modality, and
  ScheduledStationAETitle; blank query fields are wildcards
- C-FIND query, match, and completion logging

Required JSON fields for a returnable entry:

- `PatientID`
- `PatientName`
- `AccessionNumber`
- `Modality`
- `ScheduledStationAETitle`
- `ScheduledProcedureStepDescription`

Optional safety fields:

- `Active`: defaults to `true`; set to `false` to keep an entry in JSON without
  returning it in MWL responses
- `ExpiresAt`: ISO datetime string; expired entries are skipped

Invalid entries are skipped with warning logs. One bad entry must not prevent
the MWL SCP from serving other valid entries.

## Local Update API

The MWL container also runs a small local HTTP API for controlled updates to the
JSON worklist file:

```text
GET  /health
GET  /worklist
PUT  /worklist
POST /worklist/complete
POST /worklist/cancel
```

By default Docker publishes this API only on host loopback:

```text
127.0.0.1:8055
```

Do not expose this API publicly. External access should go through a future
controlled KaosPACS Gateway or KaosEghis-PACS adapter.

Examples:

```bash
curl http://127.0.0.1:8055/health
curl http://127.0.0.1:8055/worklist
curl -X PUT http://127.0.0.1:8055/worklist \
  -H 'Content-Type: application/json' \
  --data @mwl/config/worklist.json
```

`PUT /worklist` requires the same file shape:

```json
{
  "entries": []
}
```

The API validates required fields, `Active`, and `ExpiresAt` before writing. An
invalid payload returns HTTP 400 and does not overwrite the current file. Valid
writes are atomic replacements of `WORKLIST_PATH`.

Workflow state updates identify entries by `AccessionNumber`:

```bash
curl -X POST http://127.0.0.1:8055/worklist/complete \
  -H 'Content-Type: application/json' \
  -d '{"AccessionNumber":"KAOSMWL001"}'

curl -X POST http://127.0.0.1:8055/worklist/cancel \
  -H 'Content-Type: application/json' \
  -d '{"AccessionNumber":"KAOSMWL001","CancelReason":"patient no-show"}'
```

`complete` sets `Active=false` and `CompletedAt` to the current ISO datetime.
`cancel` sets `Active=false`, `CancelledAt` to the current ISO datetime, and
optionally stores `CancelReason`. These actions do not physically delete
entries.

The MWL API only manages explicit worklist state: active, completed, cancelled,
and expired. It does not infer clinical workflow from Orthanc studies.

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
