# Troubleshooting

## Port 104 Binding Fails

Port `104` is a privileged low port. Depending on the host, binding it may
require rootful Docker, host networking, or extra capabilities.

Gateway binds `VIEWREX:104`. Orthanc is an internal backend on the Docker
network and should not publish a host DICOM port for direct modality traffic.

MWL remains separate. Gateway does not own `VIEWREX_WL:105`; the MWL service
continues to answer legacy modality C-FIND requests directly.

Check:

```bash
docker compose ps
docker compose logs orthanc
```

Also check whether another DICOM service is already bound to port `104`.

## Gateway DICOM Front Door

Gateway's C-STORE front door owns production storage traffic:

```text
AET:  VIEWREX
Bind: 0.0.0.0
Port: 104
```

Check:

```bash
docker compose logs gateway
sudo ss -ltnp | grep -E ':(104|11112)\b' || true
```

If port `104` is not owned by Gateway, check `GATEWAY_DICOM_ENABLED=true`,
`GATEWAY_DICOM_AET=VIEWREX`, `GATEWAY_DICOM_PORT=104`, and
`GATEWAY_DICOM_BIND=0.0.0.0`, then restart Gateway.

If forwarding to Orthanc fails, verify:

```text
GATEWAY_DICOM_FORWARD_ENABLED=true
ORTHANC_DICOM_HOST=orthanc
ORTHANC_DICOM_PORT=11112
ORTHANC_DICOM_AET=VIEWREX
```

Gateway writes read-only non-PHI charset/tag inspection summaries to
`/app/data/dicom_inspection.jsonl`. The guarded charset fixer is enabled by
default:

```text
GATEWAY_DICOM_CHARSET_FIX_ENABLED=true
GATEWAY_DICOM_CHARSET_FIX_MODE=iso_ir_149_to_utf8
```

Fix reports are written to `/app/data/dicom_charset_fix.jsonl`. Gateway only
fixes declared `ISO_IR 149` or `ISO 2022 IR 149` acquisition DICOM. It skips
missing charset, unknown charset, and `ISO_IR 192`. Gateway does not apply
broad charset guessing, private tag edits, pixel edits, UID edits, PatientID
edits, AccessionNumber edits, Modality edits, or PHI logging.

If queue rows appear unexpectedly, verify:

```text
GATEWAY_DICOM_QUEUE_ENABLED=false
```

The queue foundation is persisted at `/app/data/gateway_queue.sqlite3` under
the Gateway data mount. Queue enqueueing is disabled by default. The retry
worker is also disabled by default with:

```text
GATEWAY_QUEUE_WORKER_ENABLED=false
```

When enabled, queueing records pending rows after successful local stores and
the worker can forward queued files to Orthanc. Direct mode remains the default:

```text
GATEWAY_DICOM_FORWARD_MODE=direct
```

Queue mode requires both queueing and the worker:

```text
GATEWAY_DICOM_FORWARD_MODE=queue
GATEWAY_DICOM_QUEUE_ENABLED=true
GATEWAY_QUEUE_WORKER_ENABLED=true
```

Queue mode does not call MWL completion yet.

Repeated C-STORE sends with the same `SOPInstanceUID` should not create
duplicate queue rows. The queue uses a partial unique index on `SOPInstanceUID`
and duplicate enqueue attempts return the existing row. A duplicate of a
completed row is not reset to pending.

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

If the modality uses a different called AET, Gateway rejects the association or
the workflow will not match the legacy configuration.

## Korean Text Display Issue

Gateway and MWL preserve Korean order/worklist text as UTF-8 JSON. MWL DICOM
C-FIND responses use `MWL_DICOM_CHARACTER_SET`, which defaults to:

```text
SpecificCharacterSet=ISO 2022 IR 149
```

This is the legacy Korean DICOM character set used for BMD compatibility. If a
modality is verified to support UTF-8 MWL correctly, set
`MWL_DICOM_CHARACTER_SET=ISO_IR 192` and redeploy MWL.

`SpecificCharacterSet=ISO_IR 149` has also been observed in
modality-produced acquisition DICOM. The current runtime reports those samples
with `needs_charset_review=true` in `/app/data/dicom_inspection.jsonl`. It
rewrites acquisition DICOM only when the guarded fixer rule matches.

The final charset/tag handling point is Gateway ingestion, not Orthanc or MWL.
Gateway defaults to the validated conservative fixer. It should expand Korean
charset/tag fixes only after validation with real samples and a rollback plan.
To disable the current fixer:

```bash
# set these in .env
# GATEWAY_DICOM_CHARSET_FIX_ENABLED=false
# GATEWAY_DICOM_CHARSET_FIX_MODE=off
docker compose up -d gateway
```

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

## BMD Order Appears On X-Ray Workstation

Some legacy workstations send broad MWL C-FIND queries with blank modality and
blank `ScheduledStationAETitle`. KaosPACS MWL falls back to the DICOM calling AE
as the station filter when the query station is blank. INNOVISION should only
receive entries whose `ScheduledStationAETitle` is `INNOVISION`; BMD should only
receive entries whose `ScheduledStationAETitle` is `BMD`.

If this regresses, check MWL logs for `calling_ae`, `station_aet`, and
`matches`.

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

Gateway `/status` also reports DICOM queue counts by status and retry worker
enabled/running state when the queue DB is reachable. Those counts are
operational state only and must not contain patient demographics or dataset
contents.

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

The `gateway_dicom` block reports the production C-STORE front-door
configuration, forwarding target, queue counts, and worker state. It must not
include patient data, accession numbers, or stored DICOM content.

## Runtime Worklist Accumulates Old Entries

Completed, expired, and cancelled entries are preserved in the runtime MWL
worklist for traceability, but test entries can accumulate. Gateway provides a
protected admin cleanup endpoint:

```bash
curl -X POST http://127.0.0.1:8060/admin/worklist/prune \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  --data '{"dry_run":true,"older_than_days":7,"statuses":["completed","cancelled","expired"]}'
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

## Worklist Entry Expired

MWL marks an active entry expired when its imaging window has passed without
DICOM completion. `ExpiresAt` is the primary window. If `ExpiresAt` is missing,
MWL falls back to the scheduled imaging date.

Expired entries are marked:

```text
Active=false
ExpiredAt=<current ISO datetime>
ExpireReason=expired_without_imaging
```

They remain in `/app/data/worklist.json` until pruned, but they are not returned
to DICOM MWL C-FIND. Expiry is not source cancellation. Do not treat it as
eGHIS cancellation or deletion; source cancellations must arrive explicitly
from KaosEghis-PACS through Gateway or the internal MWL API.

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

If entries are marked `Active=false`, completed, expired, or cancelled, they
are not returned to DICOM MWL C-FIND.

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

Worklist completion is an explicit API call. The MWL service does not infer
completion from Orthanc studies.

Gateway completion currently runs in direct mode after local store, direct
forwarding, and MWL matching. Queue-mode worker forwarding does not call
completion yet.

Gateway is responsible for calling:

```text
POST /worklist/complete
```

after it successfully receives the DICOM study and forwards it to Orthanc. Do
not add DICOM completion inference to KaosEghis-PACS, and do not make MWL
communicate directly with Orthanc.
