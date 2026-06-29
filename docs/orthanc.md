# Orthanc

Orthanc is the storage, index, REST, DICOMweb, and viewer backend for
KaosPACS.

## Identity

Current transitional storage identity:

```text
AET:  VIEWREX
Port: 104
HTTP: 8042
```

Orthanc currently owns `VIEWREX:104` to keep the verified legacy modality
storage path working. This is temporary. In the final Gateway-centered
architecture, Gateway will own `VIEWREX:104` and Orthanc will move behind
Gateway as an internal backend.

Gateway is the workflow and storage integration boundary in the final
architecture. Orthanc should not receive directly from modalities, from
KaosEghis-PACS, or from MWL.

The current Gateway DICOM C-STORE skeleton is disabled by default and uses only
the loopback test identity `KAOSPACS_GW_TEST:11104` when explicitly enabled. It
does not replace Orthanc as the current `VIEWREX:104` receiver and does not
forward studies to Orthanc.

Orthanc HTTP is available for initial local testing at:

```text
http://192.168.0.200:8042
```

Authentication is disabled during initial local testing. This is not a final
security posture.

## PostgreSQL

PostgreSQL is used for Orthanc metadata/index only:

```json
{
  "EnableIndex": true,
  "EnableStorage": false
}
```

DICOM binaries are not stored in PostgreSQL.

## DICOM Storage

DICOM files are stored on the host path from `ORTHANC_STORAGE`, mounted into
the container at:

```text
/var/lib/orthanc/storage
```

Default host path:

```text
/srv/docker/kaospacs/orthanc-storage
```

In the final architecture, modalities should not send directly to Orthanc.
Gateway will receive studies, perform validated safe ingestion checks or fixes,
forward accepted studies to Orthanc, and call the MWL completion endpoint after
successful receive/forward/storage.

## Viewer Assumptions

The selected Orthanc image is expected to include common plugins such as
DICOMweb and Stone Web Viewer. Initial configuration enables DICOMweb and
requests Stone Web Viewer support when the plugin is available.

Weasis launching is future KaosPACS Web scope, not part of this initial stack.
