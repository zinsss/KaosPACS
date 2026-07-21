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

Deployment defaults preserve:

- `GATEWAY_DICOM_AET=VIEWREX`
- `GATEWAY_DICOM_PORT=104`
- `ORTHANC_DICOM_AET=VIEWREX`
- `ORTHANC_INTERNAL_DICOM_PORT=11112`
- `MWL_AET=VIEWREX_WL`
- `MWL_PORT=105`
- `MWL_DICOM_CHARACTER_SET=ISO 2022 IR 149`
- `MWL_API_PORT=8055`
- `PACS_HOST_IP=192.168.0.200`

Gateway owns `VIEWREX:104`. Orthanc DICOM is internal only on
`orthanc:11112` and has no host DICOM port published for direct modality
traffic. `VIEWREX_WL:105` remains owned by the MWL service; Gateway does not
proxy the DICOM MWL SCP.

Gateway is present as a workflow HTTP service in front of MWL. Gateway HTTP
host publishing is controlled by:

```text
GATEWAY_HTTP_BIND
GATEWAY_HTTP_PORT
```

For same-host deployments, `GATEWAY_HTTP_BIND` may be `127.0.0.1`. For
cross-machine KaosEghis-PACS integration, set `GATEWAY_HTTP_BIND=0.0.0.0`, keep
`GATEWAY_API_TOKEN` configured, and restrict access to trusted clinic hosts
with the firewall. The MWL HTTP API remains published on host loopback only at
`127.0.0.1:8055`; do not expose MWL API on the LAN.

MWL JSON/API traffic remains UTF-8. The DICOM MWL C-FIND response charset is
controlled separately by `MWL_DICOM_CHARACTER_SET`; the clinic default is
`ISO 2022 IR 149` for legacy Korean BMD compatibility.

Useful endpoints:

```text
http://127.0.0.1:8060/health
GET http://127.0.0.1:8060/status
http://127.0.0.1:8060/worklist
GET http://127.0.0.1:8060/imaging/worklist
POST http://127.0.0.1:8060/orders/upsert
POST http://127.0.0.1:8060/orders/cancel
POST http://127.0.0.1:8060/admin/worklist/prune
http://192.168.0.200/emr.php
```

Production order integrations should send normalized order events to Gateway,
and Gateway calls the internal MWL API. Raw Gateway `/worklist` endpoints
remain internal/development helpers. Gateway receives production DICOM studies
on `VIEWREX:104`; it does not poll eGHIS.

KaosPACS Web is an Orthanc study browser, Weasis launcher, and patient-context
document upload surface. It is configured by:

```text
WEB_HTTP_BIND=0.0.0.0
WEB_PORT=8070
WEB_LEGACY_HTTP_BIND=0.0.0.0
WEB_LEGACY_PORT=80
WEB_ORTHANC_PUBLIC_URL=http://192.168.0.200:8042
WEASIS_DICOMWEB_URL=http://192.168.0.200:8042/dicom-web
WEB_GATEWAY_URL=http://gateway:8060
WEB_STUDY_LIMIT=100
```

The web container talks to Orthanc internally at `http://orthanc:8042` and to
Gateway internally at `http://gateway:8060`. KaosEghis should embed the Web
admin page at:

```text
http://<pacs-host>:8070/imaging/worklist
```

Legacy eGHIS PACS buttons that cannot include a port should continue to use:

```text
http://<pacs-host>/emr.php?m_patid=<chart_no>
```

The embedded Web admin page provides fallback operator actions for active rows:
Done, Cancel, and Delete. Delete is implemented as a soft operator cancellation
through Gateway, not physical DICOM or audit deletion.
Browsers open `http://192.168.0.200/emr.php`. The Weasis buttons use the
configured DICOMweb URL, so client workstations must be able to reach Orthanc
HTTP at `192.168.0.200:8042` and must have Weasis installed and registered for
the `weasis://` protocol.

When eGHIS opens
`http://192.168.0.200/emr.php?m_patid=<chart_no>&m_patname=<name>&m_dob=<yyyymmdd>&m_sex=<M|F|O>`,
KaosPACS Web filters studies to that chart number, displays chart
number/name/DOB/sex from the launch context, and shows a file upload control on
the same patient page. V1 upload accepts repeated pasted clipboard images and
dragged or file-picked JPG, PNG, and PDF files, creates DICOM objects with
`PatientID=<chart_no>` plus the supplied name/DOB/sex when present, and uploads
them to Orthanc. Clipboard paste remains image-only. Queued uploads can be
removed or reordered with Move up/Move down. Images become DICOM Secondary
Capture objects; each PDF page is rendered into a separate DICOM Secondary
Capture image so it can be viewed directly in Orthanc/Web/Weasis. PDF uploads
are limited to 10 pages. Pasted clipboard images do not need to be saved as
temporary desktop files. It does not ask the
operator to manually type patient demographics. The upload size limit is
controlled by:

```text
WEB_UPLOAD_MAX_BYTES=26214400
WEB_AUTH_USERNAME=kaospacs
WEB_AUTH_PASSWORD=<random-password>
WEB_ADMIN_AUTH_REQUIRED=false
WEB_EMR_AUTH_REQUIRED=false
KAOSEGHIS_PACS_BASE_URL=http://192.168.0.100:8765
KAOSPACS_INTEGRATION_TOKEN=<shared-token>
KAOSEGHIS_PACS_TIMEOUT_SECONDS=3
WEB_LOCAL_PATIENT_CONTEXT_URL=http://127.0.0.1:8765
```

Web does not own MWL state, infer completion/expiry, receive modality DICOM, or
change Gateway receive/forward/charset behavior. Web upload writes generated
JPG/PNG/PDF-derived DICOM directly to Orthanc for the launched patient context.
Set `WEB_AUTH_PASSWORD` in `.env` for browser Basic Auth before exposing Web on
the clinic LAN. Do not commit the production password. Leave
`WEB_AUTH_PASSWORD` empty only for development. `GET /health` remains open for
Docker health checks.

`WEB_ADMIN_AUTH_REQUIRED=false` lets KaosEghis embed `/imaging/worklist`
without a browser Basic Auth retry loop. `WEB_EMR_AUTH_REQUIRED=false` lets the
legacy EMR launch `/emr.php` without a Basic Auth retry loop; set it to `true`
only if the EMR desktop can handle Basic Auth. The embedded admin page still
performs state-changing actions through Gateway using the internal bearer token.

`KAOSEGHIS_PACS_BASE_URL` is optional. When set, KaosPACS Web can call the
KaosEghis-PACS read-only patient-context endpoint for chart-only EMR launches:

```text
GET /api/kaospacs/patient-context?chart_no=<chart_no>
Authorization: Bearer <KAOSPACS_INTEGRATION_TOKEN>
```

This fallback is used only when launch parameters and Orthanc/DICOM metadata are
missing name, DOB, or sex. Web fills only blank fields before rendering and
before generating upload DICOM metadata. It must not fetch eGHIS orders,
reports, diagnoses, notes, phone numbers, addresses, resident IDs, or any other
EMR data.

Before using the server-to-server KaosEghis-PACS fallback, Web checks Gateway
`/imaging/worklist` for already-synced order demographics. If the PACS server
cannot reach `192.168.0.100:8765`, the page can still use
`WEB_LOCAL_PATIENT_CONTEXT_URL=http://127.0.0.1:8765` from the EMR desktop
browser. That browser-local path must not embed the shared bearer token in HTML;
KaosEghis-PACS should allow loopback-only requests without a token while still
requiring bearer authentication for non-loopback callers.

Gateway DICOM front-door settings:

```text
GATEWAY_DICOM_ENABLED=true
GATEWAY_DICOM_AET=VIEWREX
GATEWAY_DICOM_BIND=0.0.0.0
GATEWAY_DICOM_PORT=104
GATEWAY_DICOM_STORAGE_DIR=/app/data/dicom-inbox
GATEWAY_QUEUE_DB=/app/data/gateway_queue.sqlite3
GATEWAY_DICOM_QUEUE_ENABLED=false
GATEWAY_QUEUE_WORKER_ENABLED=false
GATEWAY_QUEUE_POLL_INTERVAL_SECONDS=5
GATEWAY_QUEUE_MAX_ATTEMPTS=10
GATEWAY_DICOM_FORWARD_MODE=direct
GATEWAY_DICOM_FORWARD_ENABLED=true
ORTHANC_DICOM_HOST=orthanc
ORTHANC_DICOM_PORT=11112
ORTHANC_DICOM_AET=VIEWREX
GATEWAY_FORWARDING_AET=KAOSPACS_GW
GATEWAY_DICOM_FORWARD_TIMEOUT_SECONDS=10
GATEWAY_DICOM_INSPECTION_ENABLED=true
GATEWAY_DICOM_INSPECTION_REPORT_PATH=/app/data/dicom_inspection.jsonl
GATEWAY_DICOM_CHARSET_FIX_ENABLED=true
GATEWAY_DICOM_CHARSET_FIX_MODE=iso_ir_149_to_utf8
GATEWAY_DICOM_CHARSET_FIX_REPORT_PATH=/app/data/dicom_charset_fix.jsonl
```

Gateway stores incoming datasets locally, writes a read-only non-PHI
charset/tag inspection summary, forwards datasets to Orthanc, and then
matches/completes the MWL item in direct mode. The charset fixer is enabled by
default for the narrow `iso_ir_149_to_utf8` rule. It processes declared
`ISO_IR 149` or `ISO 2022 IR 149` acquisition DICOM, and the validated
INNOVISION missing-charset EUC-KR display-text pattern. It skips missing
charset with ASCII/no Korean-like text, unknown charset, and `ISO_IR 192`. When
a fix applies, Gateway keeps the original received file and writes a normalized forwarding copy under
`/app/data/dicom-inbox/forwarded`. It does not perform broad charset guessing,
private tag edits, pixel edits, UID edits, PatientID edits, AccessionNumber
edits, or Modality edits.

Inspection reports are JSONL records under the Gateway data mount:

```text
/app/data/dicom_inspection.jsonl
/srv/docker/kaospacs/gateway/dicom_inspection.jsonl
```

They include DICOM identifiers, declared character set, transfer syntax, text
tag presence, text VR counts, and review reasons. They must not include patient
names, patient IDs, DOB, sex, diagnosis, full datasets, or pixel data.

Charset fix reports are JSONL records under the Gateway data mount:

```text
/app/data/dicom_charset_fix.jsonl
/srv/docker/kaospacs/gateway/dicom_charset_fix.jsonl
```

They contain fixed/skipped keyword names and status only, not old or new text
values. To disable charset fixing:

```bash
# set these in .env
# GATEWAY_DICOM_CHARSET_FIX_ENABLED=false
# GATEWAY_DICOM_CHARSET_FIX_MODE=off
docker compose up -d gateway
```

Orthanc internal DICOM settings:

```text
ORTHANC_DICOM_AET=VIEWREX
ORTHANC_INTERNAL_DICOM_PORT=11112
```

`docker-compose.yml` exposes the Orthanc DICOM port only to the Docker network.
Do not publish Orthanc DICOM on the host; modalities must connect to Gateway.
Compose maps `ORTHANC_INTERNAL_DICOM_PORT` into the Gateway container as
`ORTHANC_DICOM_PORT`, which is what the Gateway forwarder reads.

The Gateway DICOM forwarding queue foundation is persisted under the same
Gateway data mount as `/app/data/gateway_queue.sqlite3`. It is disabled by
default with `GATEWAY_DICOM_QUEUE_ENABLED=false`. Enabling it records pending
queue rows after successful local stores. The retry worker is separately
disabled by default with `GATEWAY_QUEUE_WORKER_ENABLED=false`. When explicitly
enabled, it processes queued files in the background and forwards them to
Orthanc, but it does not match worklist entries, call completion, delete local
files, or replace the current direct-forwarding path.

`GATEWAY_DICOM_FORWARD_MODE=direct` is the default.
`GATEWAY_DICOM_FORWARD_MODE=queue` requires
both `GATEWAY_DICOM_QUEUE_ENABLED=true` and
`GATEWAY_QUEUE_WORKER_ENABLED=true`. In queue mode, C-STORE stores locally,
enqueues a pending row, and returns success after enqueue; the retry worker
forwards later and updates queue state only. Queue mode does not call MWL
completion yet.

Retry scheduling is intentionally simple:

- attempt 1: immediate
- attempt 2: 30 seconds
- attempt 3: 60 seconds
- attempt 4 and later: 300 seconds

Rows that reach `GATEWAY_QUEUE_MAX_ATTEMPTS` are marked `dead_letter` and are
not deleted.

Gateway also has an internal Orthanc HTTP client configured by:

```text
ORTHANC_URL
ORTHANC_TIMEOUT_SECONDS
```

The client is currently used for `/status` reachability only. It does not send
DICOM, inspect studies, expose studies, or return PHI.

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
control for a clinic LAN or localhost deployment. It is not intended as
internet-grade security, and future authentication may evolve independently.

KaosEghis-PACS should use the protected Gateway contract:

```text
POST /orders/upsert
POST /orders/cancel
GET  /imaging/worklist
```

`/orders/upsert` accepts UTF-8 JSON and preserves Korean text in the MWL entry.
`/orders/cancel` records explicit source cancellation by `AccessionNumber`; it
does not infer cancellation from missing source rows. `/imaging/worklist`
returns the operator-facing imaging lifecycle states `active`, `completed`,
`expired`, and `cancelled` by default. `inactive` rows are returned only with
`GET /imaging/worklist?view=all`; inactive means a retained non-actionable row
with no completion, expiry, or source cancellation timestamp, and KaosEghis-PACS
must not treat it as active.

`GET /status` is protected by the same bearer token when authentication is
enabled. It is for operational visibility only and reports dependency
reachability plus current ownership state. It must not include worklist entries,
patient names, chart numbers, accession numbers, DOB, sex, diagnosis, EMR
notes, tokens, Authorization headers, or full payloads.

`POST /admin/worklist/prune` is a protected runtime worklist cleanup endpoint.
It defaults to `dry_run=true`, removes only inactive entries matching requested
statuses, and never removes `Active=true` entries. The default statuses are
completed, cancelled, and expired. Expired pruning uses `ExpiredAt`, which MWL
sets when an active entry passes its imaging window without completion. The
response is a summary only and may include removed accession numbers, but not
patient names, chart numbers, DOB, sex, diagnosis, EMR notes, or full worklist
entries. This endpoint does not prune the MWL audit DB or Gateway audit DB.

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

The Gateway queue DB is operational state, not audit. It stores only DICOM
identifiers, accession number, modality, local file path, status, attempts, and
retry timing fields. It must not store patient demographics, clinical notes, or
full dataset payloads.

MWL runtime paths:

- `WORKLIST_SEED_PATH=/app/config/worklist.json`
- `WORKLIST_PATH=/app/data/worklist.json`
- `MWL_AUDIT_DB=/app/data/mwl_audit.sqlite3`
- `MWL_DATA=/srv/docker/kaospacs/mwl`

The checked-in seed file is mounted read-only into `/app/config`. Runtime MWL
state and the audit database are stored under `/app/data`, persisted on the host
under `/srv/docker/kaospacs/mwl`.

MWL automatically marks stale active entries expired before serving
`GET /worklist` or DICOM C-FIND. `ExpiresAt` is the primary imaging window. If
it is missing, MWL uses the scheduled imaging date as the fallback window.
Expired entries remain in `/app/data/worklist.json` as `Active=false` with
`ExpiredAt` and `ExpireReason=expired_without_imaging`; they are not returned
to modalities.

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
curl -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  http://127.0.0.1:8060/imaging/worklist
curl -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  'http://127.0.0.1:8060/imaging/worklist?view=all'
curl http://127.0.0.1:8070/health
curl http://127.0.0.1:8070/imaging/worklist
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
  --data '{"dry_run":true,"older_than_days":7,"statuses":["completed","cancelled","expired"]}'
docker compose logs mwl
```

Current DICOM storage checks target Gateway on `192.168.0.200:104`, AET
`VIEWREX`. Orthanc receives forwarded datasets internally at `orthanc:11112`,
AET `VIEWREX`, and should not accept direct modality traffic from the LAN. MWL
checks continue to target MWL on `192.168.0.200:105`, AET `VIEWREX_WL`, and
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
