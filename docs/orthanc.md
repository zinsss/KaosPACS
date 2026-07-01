# Orthanc

Orthanc is the storage, index, REST, DICOMweb, and viewer backend for
KaosPACS.

## Identity

Internal storage identity:

```text
AET:  VIEWREX
Port: 11112
HTTP: 8042
```

Gateway owns the legacy modality-facing storage identity `VIEWREX:104`.
Orthanc no longer publishes a host DICOM port for direct modality traffic.
Gateway forwards accepted studies to Orthanc on the internal Docker network at
`orthanc:11112`, AET `VIEWREX`.

Gateway is the workflow and storage integration boundary. Orthanc should not
receive directly from modalities, from KaosEghis-PACS, or from MWL.

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

Modalities should not send directly to Orthanc. Gateway receives studies,
stores a temporary copy, forwards the unchanged dataset to Orthanc, and calls
the MWL completion endpoint after successful receive/forward/storage and MWL
matching.

## Viewer Assumptions

The selected Orthanc image is expected to include common plugins such as
DICOMweb and Stone Web Viewer. Initial configuration enables DICOMweb and
requests Stone Web Viewer support when the plugin is available.

Weasis launching is future KaosPACS Web scope, not part of this initial stack.
