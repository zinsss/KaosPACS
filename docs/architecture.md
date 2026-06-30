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

The current implementation is transitional and intentionally keeps the verified
Orthanc + MWL runtime stable. It contains:

- Orthanc temporarily exposed as the modality-facing DICOM Storage SCP at
  `VIEWREX:104`.
- PostgreSQL as the Orthanc metadata/index database.
- Host-mounted file storage for DICOM binaries.
- KaosPACS MWL SCP at `VIEWREX_WL:105`.
- MWL local HTTP API bound to `127.0.0.1:8055`.
- Active MWL JSON state at `/app/data/worklist.json`, initialized from the
  read-only seed `/app/config/worklist.json`.
- Minimal MWL SQLite audit database at `/app/data/mwl_audit.sqlite3`.
- KaosPACS-owned MWL expiry for active entries that pass their imaging window
  without DICOM completion.
- Gateway localhost-only workflow API on `127.0.0.1:8060`, accepting
  normalized order events and proxying validated worklist requests to the
  internal MWL API.
- Optional Gateway shared bearer-token authentication through
  `GATEWAY_API_TOKEN` for workflow endpoints. `GET /health` stays public.
- Gateway workflow audit SQLite DB at `/app/data/gateway_audit.sqlite3`,
  persisted under `/srv/docker/kaospacs/gateway`.
- Gateway internal Orthanc HTTP client using `ORTHANC_URL` for operational
  reachability checks and future Gateway-to-Orthanc integration.
- Gateway disabled DICOM C-STORE skeleton configured as
  `KAOSPACS_GW_TEST:11104` on `127.0.0.1` for local tests only.

## Current Transitional Boundary

```text
KaosPACS MWL API / JSON
  -> MWL service VIEWREX_WL:105
  -> modality worklist
  -> modality acquisition
  -> Orthanc storage VIEWREX:104
```

The MWL API is local-only by default and manages explicit worklist state:
active, completed, cancelled, and expired. It does not infer workflow from
Orthanc studies.

Expiry is an internal imaging lifecycle state. It marks stale active entries
`Active=false` with `ExpiredAt` and
`ExpireReason=expired_without_imaging`. It is not a source cancellation, source
deletion, or eGHIS status inference.

Orthanc owning `VIEWREX:104` is a temporary runtime stage, not the final
architecture. Gateway does not bind port `104`, does not use AET `VIEWREX`,
and does not receive production C-STORE traffic. The current Gateway DICOM
listener is disabled by default and is only a loopback skeleton for test
datasets when explicitly enabled.

The current Gateway Orthanc client calls Orthanc HTTP only. It is used for
operational reachability in `/status` and as a future integration skeleton. It
does not send DICOM to Orthanc, inspect studies, expose studies, or return PHI.

The disabled Gateway C-STORE skeleton stores explicitly tested datasets in
`/app/data/dicom-inbox` only when `GATEWAY_DICOM_ENABLED=true`. Test-mode
forwarding from the local inbox to Orthanc is available only when
`GATEWAY_DICOM_FORWARD_ENABLED=true` and
`GATEWAY_DICOM_FORWARD_MODE=direct`. A persistent DICOM forwarding queue
foundation exists at `/app/data/gateway_queue.sqlite3`, but it is disabled by
default with `GATEWAY_DICOM_QUEUE_ENABLED=false`. A background retry worker can
be separately enabled with `GATEWAY_QUEUE_WORKER_ENABLED=true`; it forwards
queued files to Orthanc and updates queue state. `direct` mode is the default
and preserves the existing test flow: after successful local storage and
optional direct forwarding, Gateway fetches the active MWL worklist and
attempts a deterministic match by `AccessionNumber`, `RequestedProcedureID`,
then `ScheduledProcedureStepID`. If the match succeeds and has an accession
number, Gateway calls MWL completion. `queue` mode is test-mode only: C-STORE
stores locally, enqueues, returns success after enqueue, and the worker
forwards later. Queue mode does not match studies or call MWL completion yet.
It does not modify datasets, inspect or fix Korean charset issues, or expose
stored files over HTTP.

## Final Gateway-Centered Boundary

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

In the final architecture, Gateway owns the legacy storage identity:

```text
PACS IP:          192.168.0.200
Storage SCP AET: VIEWREX
Storage SCP port: 104
```

Orthanc moves behind Gateway as an internal storage, index, REST, DICOMweb, and
viewer backend. Orthanc should not remain the final modality-facing owner of
`VIEWREX:104`.

Gateway is the single workflow and storage integration boundary. It is not the
MWL DICOM SCP and does not own `VIEWREX_WL:105`. Legacy modalities continue to
query MWL directly using DICOM C-FIND. Gateway accepts normalized order events
from KaosEghis-PACS at `POST /orders/upsert` and `POST /orders/cancel`, then
converts those events into internal MWL API updates. Raw `/worklist` Gateway
endpoints remain internal/development helpers for now.

KaosEghis-PACS authenticates to Gateway with `Authorization: Bearer <token>`
when `GATEWAY_API_TOKEN` is configured. This shared token is a simple
localhost/clinic LAN control, not internet-grade security. Leaving the token
unset disables Gateway authentication for development only.

## Boundaries

Orthanc is storage, index, REST, DICOMweb, and viewer plugin infrastructure. It
should stay boring. In the final architecture its DICOM receive path is behind
Gateway, not directly exposed as the legacy modality endpoint.

Business logic belongs outside Orthanc:

- Gateway: modality-facing DICOM Storage SCP, safe DICOM ingress inspection,
  optional charset/tag fixes after validation, forwarding to Orthanc,
  normalized order event validation, worklist create/update/cancel through the
  MWL API, and MWL completion calls after successful storage/forwarding.
  Current Gateway audit stores only workflow event metadata and accession
  numbers, not demographics or full payloads. Current Gateway Orthanc HTTP
  client usage is limited to non-PHI reachability/future-integration
  scaffolding. Current Gateway DICOM C-STORE usage is disabled test scaffolding
  only; optional forwarding is test-mode only and is not the production
  `VIEWREX:104` ingress. The forwarding queue and retry worker are operational
  infrastructure only. Queue mode is explicit test scaffolding and does not
  call MWL completion. Current MWL completion is limited to matched test-mode
  direct DICOM receives.
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
