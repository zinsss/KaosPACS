# Workflow

## Goal

KaosPACS replaces the expired ViewRex PACS while keeping the same production
identity for legacy devices.

## Current Storage Path

INNOVISION CR has already been verified sending images to Orthanc when Orthanc
impersonates ViewRex:

```text
IP:   192.168.0.200
AET:  VIEWREX
Port: 104
```

The current stack keeps that storage identity working through Gateway.
Gateway owns `VIEWREX:104`; Orthanc receives forwarded datasets internally on
the Docker network.

Gateway exposes workflow API endpoints in front of MWL. Its HTTP host binding
is deployment-configurable: same-host deployments may publish it on
`127.0.0.1`, while cross-machine KaosEghis-PACS integration should publish it
on `0.0.0.0` with bearer-token auth and firewall restriction. Gateway accepts
normalized order events at `POST /orders/upsert` and `POST /orders/cancel` for
future KaosEghis-PACS integration. It also binds the production DICOM identity
and participates in production image ingestion.

Gateway receives C-STORE as `VIEWREX:104`, stores files in
`/app/data/dicom-inbox`, records a read-only charset/tag inspection report, and
forwards datasets to Orthanc on the internal DICOM port. By default, forwarding
is unchanged/inspection-only. If `GATEWAY_DICOM_CHARSET_FIX_ENABLED=true` and
`GATEWAY_DICOM_CHARSET_FIX_MODE=iso_ir_149_to_utf8`, Gateway may write a
normalized forwarding copy under `/app/data/dicom-inbox/forwarded` for datasets
declaring `ISO_IR 149`. A persistent queue foundation can be enabled with
`GATEWAY_DICOM_QUEUE_ENABLED=true`, which records pending queue rows after
successful local stores. A retry worker can be separately enabled with
`GATEWAY_QUEUE_WORKER_ENABLED=true`; it forwards queued files to Orthanc and
updates queue state. `direct` mode remains the default active path. After
successful local storage and direct forwarding, Gateway reads the active MWL
worklist and attempts a deterministic match. If the match succeeds and has an
accession number, Gateway calls `POST /worklist/complete`. Queue mode stores
locally, enqueues, returns success after enqueue, and the worker forwards later.
Queue mode does not match or complete worklists yet. If the opt-in charset
fixer applies, queue mode enqueues the normalized forwarding copy. Gateway does
not perform broad charset guessing, private tag edits, pixel edits, UID edits,
PatientID edits, AccessionNumber edits, or metadata rewriting.

Gateway appends non-PHI DICOM inspection summaries to:

```text
/app/data/dicom_inspection.jsonl
```

The report records DICOM identifiers, declared character set, transfer syntax,
text tag presence, text VR counts, and review reasons. It does not record
PatientName values, PatientID values, DOB, sex, diagnosis, physician names,
institution names, full datasets, or pixel data.

When the optional charset fixer is enabled, Gateway also appends non-PHI fix
reports to:

```text
/app/data/dicom_charset_fix.jsonl
```

The only current fix mode is `iso_ir_149_to_utf8`. It is conservative and
touches only approved display text fields. It never rewrites PatientID,
AccessionNumber, Modality, UIDs, pixel data, private tags, or unknown text
tags. Disable the fixer and restart Gateway to return to inspection-only
behavior.

Gateway records minimal workflow audit events for worklist API calls in its own
SQLite database. This audit is separate from the MWL audit DB and stores only
metadata such as event type, request path, accession number, status, success,
error code, and timestamp. It does not store patient demographics or full
worklist payloads.

## BMD Path

OsteoPro BMD can send images to Orthanc storage, and normal scheduled workflow
uses the KaosPACS MWL server:

```text
AET:  VIEWREX_WL
Port: 105
```

The MWL service reads active worklist state from:

```text
/app/data/worklist.json
```

On first startup, this runtime file is initialized from the read-only seed:

```text
/app/config/worklist.json
```

The local MWL API manages the active worklist:

```text
GET  /health
GET  /worklist
PUT  /worklist
POST /worklist/complete
POST /worklist/cancel
```

The MWL API is published on host loopback only at `127.0.0.1:8055` and should
not be exposed directly to external systems. Production workflow requests
should go through Gateway on port `8060`.

Gateway's production-facing order endpoints are:

```text
POST /orders/upsert
POST /orders/cancel
```

KaosEghis-PACS should send normalized order events to these endpoints rather
than raw MWL JSON. Gateway validates those events, converts them into MWL
entries, and updates the internal MWL API. Raw Gateway `/worklist` endpoints
remain internal/development helpers.

KaosEghis-PACS operator UI should read imaging lifecycle state from Gateway:

```text
GET /imaging/worklist
```

This endpoint returns flat rows plus counts using derived state:

- `cancelled`: `CancelledAt` is present
- `completed`: `CompletedAt` is present
- `expired`: `ExpiredAt` is present
- `active`: `Active=true`

By default, the endpoint returns only `active`, `completed`, `expired`, and
`cancelled` rows. Retained rows that are not active and have no completion,
expiry, or cancellation timestamp are `inactive`; they are available only with
`GET /imaging/worklist?view=all` for reconciliation. KaosEghis-PACS must not
treat inactive rows as active orders.

The UI should not infer imaging state from raw `public.mwl`, direct eGHIS
tables, MWL internals, or DICOM C-FIND. Lower-level `GET /worklist` remains a
temporary compatibility, reconcile, and debug endpoint.

Completed, expired, or cancelled entries are kept in JSON and marked
`Active=false`; they are not physically deleted and are not returned in DICOM
MWL C-FIND responses.

KaosPACS owns imaging lifecycle state only:

- `CompletedAt`: Gateway received/forwarded/matched a DICOM study and called
  MWL completion.
- `ExpiredAt`: MWL entry passed its imaging window without completion.

KaosEghis-PACS owns source/business order state:

- create
- update
- cancel
- delete
- restore/reactivate

Cancelled means an explicit source/business cancellation or deletion arrived
through Gateway or the internal MWL API. KaosPACS must not infer source
cancellation or deletion by polling eGHIS or `public.mwl`.

Gateway is the expected caller of `POST /worklist/complete` after it has
successfully received and forwarded a study to Orthanc. KaosEghis-PACS sends
normalized order events to Gateway; it should not call MWL directly in
production or infer DICOM study completion.

The MWL audit database is:

```text
/app/data/mwl_audit.sqlite3
```

It stores minimal PACS-side metadata only. It does not store patient name, DOB,
sex, resident ID, phone, address, diagnosis, or EMR notes.

MWL expiry records a minimal `worklist_expired` event with accession number
only. It does not store patient demographics or full worklist payloads.

## Current Clinical Flow

```text
KaosPACS MWL JSON/API
  -> KaosPACS MWL VIEWREX_WL:105
  -> modality selects scheduled patient
  -> modality acquires image
  -> Gateway receives DICOM as VIEWREX:104
  -> Gateway forwards original or explicitly normalized DICOM to Orthanc
  -> Orthanc stores DICOM
```

## Gateway-Centered Flow

Order path:

```text
eGHIS order
  -> KaosEghis-PACS normalizes order
  -> KaosPACS Gateway POST /orders/upsert or POST /orders/cancel
  -> Gateway creates/updates/cancels via KaosPACS MWL API
  -> MWL active runtime worklist
```

Modality worklist path:

```text
Legacy modality
  -> C-FIND to KaosPACS MWL VIEWREX_WL:105
  -> modality selects scheduled patient
```

Image path:

```text
Legacy modality
  -> modality acquires image
  -> Gateway receives DICOM as VIEWREX:104
  -> Gateway stores a local copy without modifying the dataset
  -> Gateway forwards study to Orthanc internal backend
  -> Gateway calls POST /worklist/complete
  -> future KaosPACS Web / Weasis opens study
```

Current default direct-mode Gateway DICOM flow:

```text
Gateway C-STORE VIEWREX:104
  -> store locally in /app/data/dicom-inbox
  -> inspect charset/tag presence without modifying the dataset
  -> optionally write normalized forwarding copy when charset fixer is enabled
  -> optionally enqueue pending forwarding row when queue is enabled
  -> forward selected original/normalized dataset to Orthanc
  -> GET active MWL worklist
  -> match by AccessionNumber, RequestedProcedureID, ScheduledProcedureStepID
  -> POST /worklist/complete when matched accession is present
  -> STOP
```

Completion failure is logged and audited but does not reject a DICOM object
that was already stored and forwarded successfully.

Optional queue-mode Gateway DICOM flow:

```text
Gateway C-STORE VIEWREX:104
  -> store locally in /app/data/dicom-inbox
  -> inspect charset/tag presence without modifying the dataset
  -> optionally write normalized forwarding copy when charset fixer is enabled
  -> enqueue pending forwarding row
  -> C-STORE returns success after enqueue
  -> retry worker forwards to Orthanc
  -> queue row becomes completed, failed, or dead_letter
  -> STOP
```

Queue mode is not active by default and does not replace the current
direct-forwarding path. Queue-mode worker forwarding does not match MWL
entries or call completion yet. Queue enqueueing is idempotent by
`SOPInstanceUID`, so repeated sends of the same instance reuse the existing
queue row instead of creating duplicate pending rows.

Do not add eGHIS DB polling to KaosPACS itself. eGHIS integration belongs in
KaosEghis-PACS. In production, KaosEghis-PACS sends worklist events to Gateway
rather than calling MWL directly. MWL must not connect directly to eGHIS, must
not communicate directly with Orthanc, and must not decide DICOM completion
from image storage. Gateway is the only component that understands both
workflow state and image state.

MWL expiry is not source cancellation. It only means the local imaging worklist
window passed without a matching DICOM completion. Source cancellation, source
deletion, and restoration must arrive as explicit KaosEghis-PACS/Gateway
events.
