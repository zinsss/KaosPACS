# Modality Worklist

KaosPACS MWL is a DICOM Modality Worklist SCP for legacy modality workflow. It
owns the worklist identity, active runtime worklist, local MWL API, and minimal
audit database. It is not a workflow engine and does not connect directly to
eGHIS.

## Fixed Production Identity

```text
AET:  VIEWREX_WL
Port: 105
```

Legacy modalities expect this identity. Production behavior must preserve it.

## Current Milestone

The checked-in seed worklist is:

```text
mwl/config/worklist.json
```

At runtime, the container copies this seed into the persistent data directory
only when the runtime file does not already exist:

```text
WORKLIST_SEED_PATH=/app/config/worklist.json
WORKLIST_PATH=/app/data/worklist.json
```

`/app/config` is mounted read-only. API writes must go to `/app/data`, never to
the seed file.

This JSON file, the local MWL HTTP API, and a minimal SQLite audit database are
the current PACS-side integration boundary. KaosPACS remains EMR-agnostic: it
does not connect to eGHIS and contains no eGHIS database code.
KaosEghis-PACS is responsible for reading eGHIS with read-only access,
normalizing orders, and sending worklist events to Gateway. In production,
Gateway validates those events and updates MWL through the KaosPACS MWL API.

The JSON and HTTP API stay UTF-8. DICOM C-FIND responses use the configured MWL
DICOM character set:

```text
MWL_DICOM_CHARACTER_SET=ISO 2022 IR 149
```

This legacy Korean setting is the clinic default for BMD compatibility. It
causes outgoing MWL DICOM values to be encoded consistently with
`SpecificCharacterSet=ISO 2022 IR 149`. Set `MWL_DICOM_CHARACTER_SET=ISO_IR 192`
only for modalities that are verified to handle UTF-8 MWL correctly.

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
- Internal expiry of active entries that pass their imaging window without
  completion

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
- `ExpiresAt`: ISO datetime string; if this passes before DICOM completion,
  KaosPACS marks the entry expired
- `ExpiredAt`: ISO datetime set by KaosPACS when an active entry expires
- `ExpireReason`: currently `expired_without_imaging`

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

Do not expose this API publicly. External access should go through the
KaosPACS Gateway workflow API. KaosEghis-PACS should not call MWL directly in
production.

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

## Imaging Lifecycle State

KaosPACS and KaosEghis-PACS have separate ownership:

- KaosEghis-PACS owns source/business order state: create, update, cancel,
  delete, restore, and reactivate from eGHIS or `public.mwl`.
- KaosPACS owns imaging lifecycle state: complete and expire.

Expiry is internal to KaosPACS. Before serving `GET /worklist` or a DICOM
C-FIND, MWL checks active entries. `ExpiresAt` is the primary expiry policy. If
`ExpiresAt` is missing, MWL falls back to the scheduled imaging date. When that
effective imaging window has passed, MWL marks the entry:

```json
{
  "Active": false,
  "ExpiredAt": "2026-07-01T12:00:00+09:00",
  "ExpireReason": "expired_without_imaging"
}
```

MWL does not set `CancelledAt`, `CancelReason`, or `CompletedAt` during expiry.
Cancelled means an explicit source/business cancellation or deletion arrived
through Gateway or the internal MWL API. If a source cancellation later arrives
for an already expired entry, MWL records `CancelledAt` and `CancelReason` while
keeping `ExpiredAt` as historical trace.

MWL C-FIND returns only `Active=true` entries. Completed, expired, and
cancelled entries can remain in `/app/data/worklist.json`, but modalities will
not see them.

Gateway also provides a protected admin cleanup endpoint for the runtime
worklist:

```text
POST /admin/worklist/prune
```

This endpoint calls the MWL API through Gateway. It defaults to `dry_run=true`
and never removes `Active=true` entries. It can remove only inactive completed,
cancelled, or expired entries matching the requested statuses and age threshold.
Expired pruning uses `ExpiredAt`, not the original `ExpiresAt` window.
The response is summary-only and may include `AccessionNumber`, but not patient
name, chart number, DOB, sex, diagnosis, EMR notes, or full worklist entries.

This is runtime JSON cleanup only. It does not delete rows from the MWL audit
database or Gateway audit database.

Completion ownership:

- KaosEghis-PACS sends normalized order events to Gateway.
- Gateway creates, updates, and cancels worklist entries through the MWL API.
- Gateway is the production caller of `POST /worklist/complete` after
  successful DICOM receive/forward/storage.
- MWL must not infer completion from Orthanc, and KaosEghis-PACS must not infer
  DICOM completion from order state.
- Gateway does not own `VIEWREX_WL:105`; legacy modalities query MWL directly
  using DICOM C-FIND.

## Audit Database

The active worklist and the audit database have different jobs:

- Active worklist JSON: `/app/data/worklist.json`, operational state used to
  answer DICOM MWL C-FIND.
- Audit SQLite DB: minimal daily/history tracking for PACS-side integration
  events.

The audit database is stored inside the MWL service by default:

```text
MWL_AUDIT_DB=/app/data/mwl_audit.sqlite3
```

Docker persists `/app/data` to:

```text
/srv/docker/kaospacs/mwl
```

The audit table stores only minimal metadata:

- `accession_number`
- `chart_no`
- `study_type`
- `modality`
- `station_aet`
- `scheduled_at`
- `status`
- `created_at`
- `updated_at`
- `completed_at`
- `cancelled_at`
- `cancel_reason`

Privacy rule: the audit DB intentionally does not store patient name, date of
birth, sex, resident ID, phone, address, diagnosis, or EMR notes. `PatientID`
from the worklist JSON is treated as the chart number for audit purposes.

`PUT /worklist` upserts audit rows by `AccessionNumber`. Complete and cancel
actions update both the JSON worklist entry and the matching audit row. Expiry
writes a minimal `worklist_expired` event with accession number only and updates
status to `expired` without adding demographics.

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

## Integration Boundary

MWL must remain EMR-agnostic. It should not derive entries from eGHIS, poll
eGHIS, contain eGHIS database code, communicate directly with Orthanc, or infer
study completion.

MWL also must not infer source cancellations or source deletions. Those are
business-order states owned by KaosEghis-PACS and delivered as explicit events
through Gateway.

KaosEghis-PACS is the EMR-aware adapter. It reads eGHIS with read-only access,
handles polling or events, normalizes orders, and sends worklist events to
Gateway. Gateway is the workflow and storage integration boundary: it updates
MWL through the local API and owns completion once it becomes the
modality-facing Storage SCP.
