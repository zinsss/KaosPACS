# Orthanc

Orthanc is the DICOM storage and index service for KaosPACS.

## Identity

Production storage identity:

```text
AET:  VIEWREX
Port: 104
HTTP: 8042
```

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

## Viewer Assumptions

The selected Orthanc image is expected to include common plugins such as
DICOMweb and Stone Web Viewer. Initial configuration enables DICOMweb and
requests Stone Web Viewer support when the plugin is available.

Weasis launching is future KaosPACS Web scope, not part of this initial stack.
