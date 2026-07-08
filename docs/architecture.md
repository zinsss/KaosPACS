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

The current implementation contains:

- Orthanc as the DICOM server and REST API.
- PostgreSQL as the Orthanc metadata/index database.
- Host-mounted file storage for DICOM binaries.
- KaosPACS MWL SCP at `VIEWREX_WL:105`.
- MWL local HTTP API bound to `127.0.0.1:8055`.
- Gateway imaging worklist API at `:8060`.
- Web imaging worklist UI at `:8070`.
- Active MWL JSON state at `/app/data/worklist.json`, initialized from the
  read-only seed `/app/config/worklist.json`.
- Minimal MWL SQLite audit database at `/app/data/mwl_audit.sqlite3`.

## Current Boundary

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

Gateway exposes an operator-facing imaging worklist plus a protected admin
completion correction endpoint for rare stuck-active cases. Manual completion
remains KaosPACS-owned imaging authority, not KaosEghis-PACS business
authority.

## Future Flow

```text
eGHIS
  -> KaosEghis-PACS / KaosPACS Gateway
  -> KaosPACS MWL API or JSON update
  -> modality worklist
  -> modality acquisition
  -> Orthanc storage VIEWREX:104
  -> KaosPACS Web / Weasis launch
```

## Boundaries

Orthanc is storage, index, DICOM networking, REST, DICOMweb, and viewer plugin
infrastructure. It should stay boring.

Business logic belongs outside Orthanc:

- Gateway / KaosEghis-PACS: eGHIS integration, launch coordination, and future
  workflow APIs. Gateway admin correction may mark imaging completion only.
- MWL: modality worklist responses, local worklist state, and minimal audit
  tracking.
- Web: browser launch, viewer routing, and EMR-facing PACS screens.
- Migration: read-only ViewRex extraction and additive import tooling.

The ViewRex replacement boundary is the modality and EMR contract, not the old
ViewRex internal workflow implementation.
