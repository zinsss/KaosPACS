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

The current implementation contains only:

- Orthanc as the DICOM server and REST API.
- PostgreSQL as the Orthanc metadata/index database.
- Host-mounted file storage for DICOM binaries.

## Future Flow

```text
eGHIS
  -> KaosPACS Gateway
  -> MWL service VIEWREX_WL:105
  -> modality worklist
  -> modality acquisition
  -> Orthanc storage VIEWREX:104
  -> KaosPACS Web / Weasis launch
```

## Boundaries

Orthanc is storage, index, DICOM networking, REST, DICOMweb, and viewer plugin
infrastructure. It should stay boring.

Business logic belongs outside Orthanc:

- Gateway: eGHIS integration, launch coordination, and future workflow APIs.
- MWL: modality worklist responses and eGHIS order-derived scheduling.
- Web: browser launch, viewer routing, and EMR-facing PACS screens.
- Migration: read-only ViewRex extraction and additive import tooling.

The ViewRex replacement boundary is the modality and EMR contract, not the old
ViewRex internal workflow implementation.
