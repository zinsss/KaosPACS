# Workflow

## Goal

KaosPACS replaces the expired ViewRex PACS while keeping the same production
identity for legacy devices.

## Current Verified Storage Path

INNOVISION CR has already been verified sending images to Orthanc when Orthanc
impersonates ViewRex:

```text
IP:   192.168.0.200
AET:  VIEWREX
Port: 104
```

The current stack keeps that storage path working through Orthanc.

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

The API is bound to `127.0.0.1:8055` by default and should not be exposed
directly to external systems.

Completed or cancelled entries are kept in JSON and marked `Active=false`; they
are not physically deleted and are not returned in DICOM MWL C-FIND responses.

The MWL audit database is:

```text
/app/data/mwl_audit.sqlite3
```

It stores minimal PACS-side metadata only. It does not store patient name, DOB,
sex, resident ID, phone, address, diagnosis, or EMR notes.

## Current Clinical Flow

```text
KaosPACS MWL JSON/API
  -> KaosPACS MWL VIEWREX_WL:105
  -> modality selects scheduled patient
  -> modality acquires image
  -> Orthanc stores DICOM
```

## Future EMR Flow

```text
eGHIS order
  -> KaosEghis-PACS / KaosPACS Gateway
  -> KaosPACS MWL API or JSON update
  -> KaosPACS MWL VIEWREX_WL:105
  -> modality selects scheduled patient
  -> modality acquires image
  -> Orthanc stores DICOM
  -> future KaosPACS Web / Weasis opens study
```

Do not add eGHIS DB polling to KaosPACS itself. eGHIS integration belongs in a
future KaosEghis-PACS adapter or Gateway component.
