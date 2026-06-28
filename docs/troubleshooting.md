# Troubleshooting

## Port 104 Binding Fails

Port `104` is a privileged low port. Depending on the host, binding it may
require rootful Docker, host networking, or extra capabilities.

In the current transitional stack, Orthanc binds `VIEWREX:104`. In the final
Gateway-centered stack, Gateway will bind `VIEWREX:104` and Orthanc will be an
internal backend. Do not change the compose port owner until Gateway is
implemented and a cutover is planned.

MWL remains separate. Gateway does not own `VIEWREX_WL:105`; the MWL service
continues to answer legacy modality C-FIND requests directly.

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

If the modality uses a different called AET, the current transitional receiver
may reject the association or the workflow may not match the legacy
configuration. Today that receiver is Orthanc. In the final architecture it
will be Gateway.

## Korean Text Display Issue

`SpecificCharacterSet=ISO_IR 149` has been observed. Initial setup does not
rewrite DICOM character sets.

The final charset/tag handling point is Gateway ingestion, not Orthanc or MWL.
Gateway should only inspect or fix Korean charset/tag issues after validation
with real samples and a rollback plan.

Compare behavior in:

- Orthanc Explorer / Stone Viewer
- Weasis
- Raw DICOM tag inspection

Document findings before proposing normalization.

## BMD Cannot Query Worklist

OsteoPro BMD normal workflow requires MWL:

```text
AET:  VIEWREX_WL
Port: 105
```

Check that the MWL container is running and listening:

```bash
docker compose ps
docker compose logs mwl
sudo ss -ltnp | grep ':105'
```

Confirm the modality is querying called AE `VIEWREX_WL` on port `105`.

## MWL API Not Reachable

The MWL API is intentionally local-only by default:

```text
127.0.0.1:8055
```

Check it from the PACS host:

```bash
curl http://127.0.0.1:8055/health
curl http://127.0.0.1:8055/worklist
```

Do not expose this API publicly. External access should go through a controlled
Gateway path in production. KaosEghis-PACS should send normalized worklist
events to Gateway rather than calling MWL directly. Gateway creates, updates,
or cancels worklist entries through the MWL API, and calls completion after
successful receive/forward.

## MWL Worklist Is Empty

The checked-in seed is mounted read-only:

```text
/app/config/worklist.json
```

The active runtime worklist is:

```text
/app/data/worklist.json
```

On first startup, the service copies the seed to the runtime file only if the
runtime file does not exist. Existing runtime files are preserved.

Check the host-persisted runtime data:

```bash
sudo ls -l /srv/docker/kaospacs/mwl
docker compose exec mwl python tools/query_mwl.py
```

If entries are marked `Active=false`, completed, cancelled, or expired, they are
not returned to DICOM MWL C-FIND.

## MWL API Write Fails

The API writes to `/app/data/worklist.json`, not `/app/config/worklist.json`.
The `/app/config` mount should be read-only.

Check rendered Compose config:

```bash
docker compose config | sed -n '/  mwl:/,/  orthanc:/p'
```

Expected paths:

```text
WORKLIST_SEED_PATH=/app/config/worklist.json
WORKLIST_PATH=/app/data/worklist.json
MWL_AUDIT_DB=/app/data/mwl_audit.sqlite3
./mwl/config:/app/config:ro
/srv/docker/kaospacs/mwl:/app/data
```

## MWL Audit DB Missing

The audit DB is created by the MWL service at:

```text
/app/data/mwl_audit.sqlite3
```

Host path:

```text
/srv/docker/kaospacs/mwl/mwl_audit.sqlite3
```

It is minimal by design and should not contain patient name, DOB, sex, resident
ID, phone, address, diagnosis, or EMR notes.

## Worklist Entry Not Completing

In the current transitional stage, worklist completion is an explicit API call.
The MWL service does not infer completion from Orthanc studies.

In the final Gateway-centered stage, Gateway is responsible for calling:

```text
POST /worklist/complete
```

after it successfully receives the DICOM study and forwards it to Orthanc. Do
not add DICOM completion inference to KaosEghis-PACS, and do not make MWL
communicate directly with Orthanc.
