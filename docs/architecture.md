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
- Gateway localhost-only workflow API on `127.0.0.1:8060`, proxying validated
  worklist requests to the internal MWL API.
- Gateway workflow audit SQLite DB at `/app/data/gateway_audit.sqlite3`,
  persisted under `/srv/docker/kaospacs/gateway`.

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

Orthanc owning `VIEWREX:104` is a temporary runtime stage, not the final
architecture. Gateway does not bind DICOM ports, receive C-STORE, or forward
to Orthanc yet.

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
query MWL directly using DICOM C-FIND. Gateway updates MWL through the local
MWL API for create, update, cancel, and completion state.

## Boundaries

Orthanc is storage, index, REST, DICOMweb, and viewer plugin infrastructure. It
should stay boring. In the final architecture its DICOM receive path is behind
Gateway, not directly exposed as the legacy modality endpoint.

Business logic belongs outside Orthanc:

- Gateway: modality-facing DICOM Storage SCP, safe DICOM ingress inspection,
  optional charset/tag fixes after validation, forwarding to Orthanc, worklist
  create/update/cancel through the MWL API, and MWL completion calls after
  successful storage/forwarding. Current Gateway audit stores only workflow
  event metadata and accession numbers, not demographics or full payloads.
- KaosEghis-PACS: eGHIS order discovery with read-only access, polling or event
  handling, normalization, and sending worklist events to Gateway. It should
  not call MWL directly in production, call Orthanc directly, or infer DICOM
  completion.
- MWL: modality worklist responses, local worklist state, local MWL API, and
  minimal audit tracking. It is not a workflow engine, must not connect
  directly to eGHIS, must not infer study completion, and must not communicate
  directly with Orthanc.
- Web: browser launch, viewer routing, and EMR-facing PACS screens.
- Migration: read-only ViewRex extraction and additive import tooling.

The ViewRex replacement boundary is the modality and EMR contract, not the old
ViewRex internal workflow implementation.
