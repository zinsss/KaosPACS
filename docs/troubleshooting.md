# Troubleshooting

## Port 104 Binding Fails

Port `104` is a privileged low port. Depending on the host, binding it may
require rootful Docker, host networking, or extra capabilities.

Check:

```bash
docker compose ps
docker compose logs orthanc
```

Also check whether another DICOM service is already bound to port `104`.

## Orthanc Cannot Connect To PostgreSQL

Check PostgreSQL health and credentials:

```bash
docker compose ps
docker compose logs postgres
docker compose logs orthanc
```

Confirm `.env` values:

- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`

## Storage Mount Permission Issue

Orthanc stores DICOM files under `ORTHANC_STORAGE`.

Default:

```text
/srv/docker/kaospacs/orthanc-storage
```

Create the directory and verify Docker can write to it:

```bash
sudo mkdir -p /srv/docker/kaospacs/orthanc-storage
docker compose logs orthanc
```

## DICOM Association Rejected

Legacy modalities must call:

```text
AET:  VIEWREX
Port: 104
IP:   192.168.0.200
```

If the modality uses a different called AET, Orthanc may reject the association
or the workflow may not match the legacy configuration.

## Korean Text Display Issue

`SpecificCharacterSet=ISO_IR 149` has been observed. Initial setup does not
rewrite DICOM character sets.

Compare behavior in:

- Orthanc Explorer / Stone Viewer
- Weasis
- Raw DICOM tag inspection

Document findings before proposing normalization.

## BMD Cannot Send Normally

OsteoPro BMD normal workflow requires MWL:

```text
AET:  VIEWREX_WL
Port: 105
```

The MWL service is not implemented in the initial stack. Storage may work, but
scheduled workflow will remain incomplete until MWL exists.
