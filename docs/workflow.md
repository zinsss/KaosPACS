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

The initial stack keeps that storage path working through Orthanc.

## BMD Path

OsteoPro BMD can send images to Orthanc storage, but normal clinical workflow
requires a working MWL server:

```text
AET:  VIEWREX_WL
Port: 105
```

The MWL service is future scope. The first milestone should be a hardcoded
patient response for BMD testing. Later milestones should derive worklist items
from read-only eGHIS order data.

## Future Clinical Flow

```text
eGHIS order
  -> read-only order polling
  -> KaosPACS MWL
  -> modality selects scheduled patient
  -> modality acquires image
  -> Orthanc stores DICOM
  -> KaosPACS Web / Weasis opens study
```
