# Architecture

KaosPACS replaces the legacy ViewRex PACS boundary while preserving the
modality-facing identity that legacy devices already know.

## Production Identity

```text
PACS IP:          192.168.0.200
Storage SCP AET: VIEWREX
Storage SCP port: 104
MWL SCP AET:     VIEWREX_WL
MWL SCP port:    105
```

These values are production compatibility requirements, not branding choices.

## Current Scope

The current implementation uses Gateway as the production DICOM Storage SCP
front door and Orthanc as the internal DICOM storage backend. It contains:

- Gateway exposed as the modality-facing DICOM Storage SCP at `VIEWREX:104`.
- Orthanc DICOM enabled only on the Docker network at `orthanc:11112`.
- PostgreSQL as the Orthanc metadata/index database.
- Host-mounted file storage for DICOM binaries.
- KaosPACS MWL SCP at `VIEWREX_WL:105`.
- MWL local HTTP API bound to `127.0.0.1:8055`.
- Active MWL JSON state at `/app/data/worklist.json`, initialized from the
  read-only seed `/app/config/worklist.json`.
- Minimal MWL SQLite audit database at `/app/data/mwl_audit.sqlite3`.
- KaosPACS-owned MWL expiry for active entries that pass their imaging window
  without DICOM completion.
- Gateway workflow API accepting normalized order events and proxying validated
  worklist requests to the internal MWL API. Gateway HTTP host publishing is
  deployment-configurable. For same-host deployments it may bind `127.0.0.1`;
  for cross-machine KaosEghis-PACS integration it should bind `0.0.0.0` with
  bearer-token auth and firewall restriction.
- Optional Gateway shared bearer-token authentication through
  `GATEWAY_API_TOKEN` for workflow endpoints. `GET /health` stays public.
- Gateway workflow audit SQLite DB at `/app/data/gateway_audit.sqlite3`,
  persisted under `/srv/docker/kaospacs/gateway`.
- Gateway internal Orthanc HTTP client using `ORTHANC_URL` for operational
  reachability checks.
- Gateway DICOM C-STORE front door configured as `VIEWREX:104` on
  `0.0.0.0`.

## Current Transitional Boundary

```text
KaosPACS MWL API / JSON
  -> MWL service VIEWREX_WL:105
  -> modality worklist
  -> modality acquisition
  -> Gateway storage VIEWREX:104
  -> Orthanc internal DICOM backend orthanc:11112
```

The MWL API is local-only by default and manages explicit worklist state:
active, completed, cancelled, and expired. It does not infer workflow from
Orthanc studies.

Expiry is an internal imaging lifecycle state. It marks stale active entries
`Active=false` with `ExpiredAt` and
`ExpireReason=expired_without_imaging`. It is not a source cancellation, source
deletion, or eGHIS status inference.

Gateway owns `VIEWREX:104` and receives production C-STORE traffic. Orthanc no
longer publishes a host DICOM port for direct modality traffic; it receives
forwarded datasets from Gateway on the internal Docker network.

The Gateway Orthanc HTTP client is used for non-PHI operational reachability in
`/status`. DICOM forwarding uses a separate C-STORE SCU targeting
`ORTHANC_DICOM_HOST`, `ORTHANC_DICOM_PORT`, and `ORTHANC_DICOM_AET`.

Gateway stores received datasets in `/app/data/dicom-inbox`, performs read-only
charset/tag inspection, applies the guarded charset fixer by default when the
declared charset matches `ISO_IR 149` or `ISO 2022 IR 149` or when the dataset
matches the validated missing-charset EUC-KR display-text pattern, forwards the selected dataset to Orthanc in direct mode,
then fetches the active MWL worklist and attempts a deterministic match by
`AccessionNumber`, `RequestedProcedureID`, then `ScheduledProcedureStepID`. If
the match succeeds and has an accession number, Gateway calls MWL completion. A
persistent DICOM forwarding queue exists at `/app/data/gateway_queue.sqlite3`;
queue mode stores locally, enqueues, returns success after enqueue, and lets
the retry worker forward later. Queue mode does not match studies or call MWL
completion yet. The charset fixer supports declared `ISO_IR 149` /
`ISO 2022 IR 149` and the validated missing-charset EUC-KR display-text pattern
to `ISO_IR 192` for approved display text fields. Gateway never
modifies UIDs, pixel data, PatientID, AccessionNumber, Modality, private tags,
or unknown text tags.

## Gateway-Centered Boundary

```text
                     eGHIS
                       │
                       ▼
                KaosEghis-PACS
                       │
                 HTTP / Events
                       │
                       ▼
               KaosPACS Gateway
                  │           │
                  │           ▼
                  │      Orthanc
                  │    (internal backend)
                  │
                  ▼
           KaosPACS MWL API
                  │
                  ▼
          MWL Service
       VIEWREX_WL :105

Legacy Modality
    │
    ├── C-FIND ─────────► MWL (VIEWREX_WL:105)
    │
    └── C-STORE ───────► Gateway (VIEWREX:104)
```

Gateway owns the legacy storage identity:

```text
PACS IP:          192.168.0.200
Storage SCP AET: VIEWREX
Storage SCP port: 104
```

Orthanc is behind Gateway as an internal storage, index, REST, DICOMweb, and
viewer backend. Orthanc is not the modality-facing owner of `VIEWREX:104`.

Gateway is the single workflow and storage integration boundary. It is not the
MWL DICOM SCP and does not own `VIEWREX_WL:105`. Legacy modalities continue to
query MWL directly using DICOM C-FIND. Gateway accepts normalized order events
from KaosEghis-PACS at `POST /orders/upsert` and `POST /orders/cancel`, then
converts those events into internal MWL API updates. Raw `/worklist` Gateway
endpoints remain internal/development helpers for now.

Gateway also exposes the operator-facing imaging lifecycle view:

```text
GET /imaging/worklist
```

KaosEghis-PACS UI should read this Gateway endpoint for imaging state. It
returns a flattened view with derived `active`, `completed`, `expired`,
or `cancelled` states and counts by default. `inactive` rows are available only
through `GET /imaging/worklist?view=all`; inactive means a retained
non-actionable row with no completion, expiry, or source cancellation timestamp,
and KaosEghis-PACS must not treat it as active. The endpoint does not poll
eGHIS or `public.mwl`; it reads the current KaosPACS MWL HTTP `/worklist`
through the Gateway/MWL boundary.

KaosEghis-PACS authenticates to Gateway with `Authorization: Bearer <token>`
when `GATEWAY_API_TOKEN` is configured. This shared token is a simple clinic
LAN or localhost control, not internet-grade security. Leaving the token unset
disables Gateway authentication for development only. The MWL HTTP API remains
host-loopback published only; cross-machine callers must use Gateway, not MWL
directly.

## Boundaries

Orthanc is storage, index, REST, DICOMweb, and viewer plugin infrastructure. It
should stay boring. Its DICOM receive path is behind Gateway, not directly
exposed as the legacy modality endpoint.

Business logic belongs outside Orthanc:

- Gateway: modality-facing DICOM Storage SCP, C-STORE association validation,
  local DICOM inbox storage, read-only charset/tag inspection, guarded
  default-on charset normalization for validated Korean acquisition DICOM,
  forwarding datasets to Orthanc, normalized order event validation, worklist
  create/update/cancel through the MWL API, operator-facing imaging lifecycle
  read API, and MWL completion calls after successful storage/forwarding.
  Current Gateway audit stores only workflow event metadata and accession
  numbers, not demographics or full payloads. Current Gateway Orthanc HTTP
  client usage is limited to non-PHI reachability. Gateway writes non-PHI
  DICOM inspection JSONL summaries under `/app/data/dicom_inspection.jsonl`
  and optional non-PHI charset-fix reports under
  `/app/data/dicom_charset_fix.jsonl`. Charset fixing is enabled by default
  for the conservative declared `ISO_IR 149` rule and validated missing-charset
  EUC-KR display-text pattern. Gateway does not perform broad charset guessing,
  tag normalization, pixel
  edits, or PHI logging. The
  forwarding queue and retry worker are operational infrastructure. Queue mode
  does not call MWL completion; completion is currently part of the direct
  forwarding path after a successful MWL match.
- KaosEghis-PACS: eGHIS order discovery with read-only access, polling or event
  handling, normalization, and sending normalized order events to Gateway. It
  should not call MWL directly in production, call Orthanc directly, or infer
  DICOM completion. It owns source/business order create, update, cancel,
  delete, restore, and reactivate events.
- MWL: modality worklist responses, local worklist state, local MWL API, and
  minimal audit tracking. It is not a workflow engine, must not connect
  directly to eGHIS, must not infer study completion, and must not communicate
  directly with Orthanc. It owns KaosPACS internal expiry for active entries
  whose imaging window passed without DICOM completion, and it must not infer
  source cancellation or deletion from eGHIS or `public.mwl`.
- Web: browser launch, viewer routing, and EMR-facing PACS screens.
- Migration: read-only ViewRex extraction and additive import tooling.

The ViewRex replacement boundary is the modality and EMR contract, not the old
ViewRex internal workflow implementation.
