# Deployment

## Paths

Repository path:

```text
/srv/projects/kaospacs
```

Persistent runtime data path:

```text
/srv/docker/kaospacs
```

Recommended host setup:

```bash
sudo mkdir -p /srv/projects
sudo mkdir -p /srv/docker/kaospacs/{orthanc-storage,postgres,logs,backups,mwl,gateway,web}
```

## Environment

Create the runtime environment file:

```bash
cp .env.example .env
```

Current transitional deployment defaults preserve:

- `ORTHANC_AET=VIEWREX`
- `ORTHANC_DICOM_PORT=104`
- `MWL_AET=VIEWREX_WL`
- `MWL_PORT=105`
- `MWL_API_PORT=8055`
- `PACS_HOST_IP=192.168.0.200`

Today, Orthanc owns `VIEWREX:104` so the working storage path remains stable.
This is not the final architecture. When Gateway is implemented, Gateway will
own `VIEWREX:104` and Orthanc will move behind Gateway as an internal backend.
Do not change `docker-compose.yml` for that future stage until Gateway exists
and the cutover is planned. `VIEWREX_WL:105` remains owned by the MWL service
in both the current and final architectures; Gateway does not proxy the DICOM
MWL SCP.

Gateway is present as a localhost-only workflow HTTP service in front of MWL.
Useful endpoints:

```text
http://127.0.0.1:8060/health
GET http://127.0.0.1:8060/status
http://127.0.0.1:8060/worklist
POST http://127.0.0.1:8060/orders/upsert
POST http://127.0.0.1:8060/orders/cancel
POST http://127.0.0.1:8060/admin/worklist/prune
```

It does not bind port `104`, receive production DICOM studies, poll eGHIS, or
change current PACS runtime behavior. Production order integrations
should send normalized order events to Gateway, and Gateway calls the internal
MWL API. Raw Gateway `/worklist` endpoints remain internal/development helpers.

Gateway also includes a disabled DICOM C-STORE skeleton for local testing only:

```text
GATEWAY_DICOM_ENABLED=false
GATEWAY_DICOM_AET=KAOSPACS_GW_TEST
GATEWAY_DICOM_BIND=127.0.0.1
GATEWAY_DICOM_PORT=11104
GATEWAY_DICOM_STORAGE_DIR=/app/data/dicom-inbox
GATEWAY_DICOM_FORWARD_ENABLED=false
ORTHANC_DICOM_HOST=orthanc
ORTHANC_DICOM_PORT=104
ORTHANC_DICOM_AET=VIEWREX
GATEWAY_FORWARDING_AET=KAOSPACS_GW
GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS=10
```

There is no Gateway DICOM port published in `docker-compose.yml` by default.
Do not use AET `VIEWREX` or port `104` for this skeleton. Orthanc remains the
current transitional owner of `VIEWREX:104`. Test-mode forwarding to Orthanc
requires both `GATEWAY_DICOM_ENABLED=true` and
`GATEWAY_DICOM_FORWARD_ENABLED=true`. Matched test-mode DICOM receives can call
MWL completion after successful storage and optional forwarding, but they still
do not perform charset fixes.

Gateway also has an internal Orthanc HTTP client configured by:

```text
ORTHANC_URL
ORTHANC_TIMEOUT_SECONDS
```

The client is currently used for `/status` reachability and future
Gateway-to-Orthanc integration only. It does not send DICOM, inspect studies,
expose studies, or return PHI.

Gateway workflow endpoints support shared bearer-token authentication through:

```text
GATEWAY_API_TOKEN
```

Generate a random token, for example:

```bash
openssl rand -hex 32
```

Do not commit the production token. If `GATEWAY_API_TOKEN` is empty or unset,
Gateway logs a warning and disables authentication for development. When the
token is set, KaosEghis-PACS and local workflow callers must send:

```text
Authorization: Bearer <token>
```

Only `GET /health` remains unauthenticated. This is a simple shared-token
control for a localhost or clinic LAN deployment. It is not intended as
internet-grade security, and future authentication may evolve independently.

`GET /status` is protected by the same bearer token when authentication is
enabled. It is for operational visibility only and reports dependency
reachability plus current ownership state. It must not include worklist entries,
patient names, chart numbers, accession numbers, DOB, sex, diagnosis, EMR
notes, tokens, Authorization headers, or full payloads.

`POST /admin/worklist/prune` is a protected runtime worklist cleanup endpoint.
It defaults to `dry_run=true`, removes only inactive entries matching requested
statuses, and never removes `Active=true` entries. The response is a summary
only and may include removed accession numbers, but not patient names, chart
numbers, DOB, sex, diagnosis, EMR notes, or full worklist entries. This endpoint
does not prune the MWL audit DB or Gateway audit DB.

Gateway writes a minimal workflow audit database at:

```text
/app/data/gateway_audit.sqlite3
```

Docker persists `/app/data` to:

```text
/srv/docker/kaospacs/gateway
```

This Gateway audit DB is separate from the MWL audit DB. It stores workflow
event metadata only: event type, request path, accession number when present,
status, success flag, error code, and timestamp. It must not store patient
demographics, clinical notes, or full payload JSON.

MWL runtime paths:

- `WORKLIST_SEED_PATH=/app/config/worklist.json`
- `WORKLIST_PATH=/app/data/worklist.json`
- `MWL_AUDIT_DB=/app/data/mwl_audit.sqlite3`
- `MWL_DATA=/srv/docker/kaospacs/mwl`

The checked-in seed file is mounted read-only into `/app/config`. Runtime MWL
state and the audit database are stored under `/app/data`, persisted on the host
under `/srv/docker/kaospacs/mwl`.

## Startup

```bash
docker compose config
docker compose up -d
docker compose ps
```

Useful checks:

```bash
curl http://127.0.0.1:8055/health
curl http://127.0.0.1:8055/worklist
curl http://127.0.0.1:8060/health
# Development only when GATEWAY_API_TOKEN is unset:
curl http://127.0.0.1:8060/status
curl http://127.0.0.1:8060/worklist
# When GATEWAY_API_TOKEN is set:
curl -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  http://127.0.0.1:8060/status
curl -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  http://127.0.0.1:8060/worklist
curl -X POST http://127.0.0.1:8060/orders/upsert \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  --data '{"ChartNo":"12345","PatientName":"TEST^PATIENT","AccessionNumber":"TEST-ORDER-1","StudyType":"BMD","Modality":"BMD","StationAET":"BMD","ScheduledAt":"2026-06-29T09:00:00+09:00","Description":"BMD"}'
curl -X POST http://127.0.0.1:8060/orders/cancel \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  --data '{"AccessionNumber":"TEST-ORDER-1","CancelReason":"test cleanup"}'
curl -X POST http://127.0.0.1:8060/admin/worklist/prune \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  --data '{"dry_run":true,"older_than_days":7,"statuses":["completed","cancelled"]}'
docker compose logs mwl
```

Current DICOM storage checks still target Orthanc on `192.168.0.200:104`, AET
`VIEWREX`. In the final Gateway-centered deployment, the same modality-facing
storage identity will be owned by Gateway and Orthanc will be internal. Current
MWL checks continue to target MWL on `192.168.0.200:105`, AET `VIEWREX_WL`, and
that ownership does not move to Gateway.

## Shutdown

```bash
docker compose down
```

Do not remove volumes or host directories unless intentionally destroying the
PACS data.

## Backup Placeholders

Backup target:

```text
/srv/docker/kaospacs/backups
```

Future backup jobs should cover:

- Orthanc DICOM file storage under `ORTHANC_STORAGE`.
- PostgreSQL database dumps from `POSTGRES_DB`.
- MWL runtime data under `/srv/docker/kaospacs/mwl`, including
  `worklist.json` and `mwl_audit.sqlite3`.
- Gateway runtime data under `/srv/docker/kaospacs/gateway`, including
  `gateway_audit.sqlite3`.
- Future Gateway DICOM quarantine/staging directories once DICOM ingress is
  implemented.
- KaosPACS configuration and operational logs.
