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

## Gateway DICOM Skeleton Unexpectedly Listening

Gateway's C-STORE skeleton is disabled by default and must not own production
storage traffic. Its test defaults are:

```text
AET:  KAOSPACS_GW_TEST
Bind: 127.0.0.1
Port: 11104
```

Check:

```bash
docker compose logs gateway
sudo ss -ltnp | grep -E ':(104|11104)\b' || true
```

If `11104` is listening unexpectedly, verify `GATEWAY_DICOM_ENABLED=false` in
the runtime environment and restart only the Gateway service. Do not change
Orthanc ownership of `VIEWREX:104` during this skeleton phase.

If test-mode forwarding unexpectedly sends studies to Orthanc, verify:

```text
GATEWAY_DICOM_FORWARD_ENABLED=false
```

Forwarding requires both `GATEWAY_DICOM_ENABLED=true` and
`GATEWAY_DICOM_FORWARD_ENABLED=true`. It is local test scaffolding only; it
does not imply Gateway owns `VIEWREX:104`. Matched test-mode DICOM receives can
call MWL completion after successful storage and optional forwarding, but they
do not apply charset fixes.

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

## Gateway Status Endpoint

Gateway exposes an operational status endpoint:

```text
GET http://127.0.0.1:8060/status
```

`GET /health` remains open for basic container health checks. `GET /status` is
protected when `GATEWAY_API_TOKEN` is configured:

```bash
curl -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  http://127.0.0.1:8060/status
```

If `/status` returns `401`, check that the `Authorization: Bearer <token>`
header matches `GATEWAY_API_TOKEN`.

The status response is operational only. It should contain dependency
reachability and ownership state, but no worklist entries, patient names, chart
numbers, accession numbers, DOB, sex, diagnosis, EMR notes, tokens, headers, or
payloads.

If a dependency reports `reachable=false`, check the corresponding service:

- `mwl_api`: `docker compose ps mwl` and `docker compose logs mwl`
- `orthanc_http`: `docker compose ps orthanc` and `docker compose logs orthanc`
- `gateway_audit_db`: host permissions for `/srv/docker/kaospacs/gateway`

The `orthanc_http` check uses Gateway's internal Orthanc HTTP client against
`ORTHANC_URL`. It checks reachability only and does not expose studies, patient
data, DICOM instances, or Orthanc response bodies through `/status`.

The `gateway_dicom` block reports only whether the disabled C-STORE skeleton is
enabled and its configured test bind/AET/port/storage directory. It must not
include patient data, accession numbers, or stored DICOM content.

## Runtime Worklist Accumulates Old Entries

Completed and cancelled entries are preserved in the runtime MWL worklist for
traceability, but test entries can accumulate. Gateway provides a protected
admin cleanup endpoint:

```bash
curl -X POST http://127.0.0.1:8060/admin/worklist/prune \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  --data '{"dry_run":true,"older_than_days":7,"statuses":["completed","cancelled"]}'
```

The default is `dry_run=true`. Review the summary before running with
`dry_run=false`.

Safety rules:

- Active entries are never removed.
- Only inactive completed, cancelled, or expired entries matching the request
  are eligible.
- Entries with unparseable timestamps are preserved.
- The response contains accession numbers only, not full worklist entries or
  patient demographics.
- This prunes only `/app/data/worklist.json`; it does not delete MWL audit DB
  rows or Gateway audit DB rows.

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
