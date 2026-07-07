# KaosPACS

KaosPACS is a Docker-based PACS replacement stack for an expired proprietary
ViewRex PACS system used with eGHIS EMR and legacy imaging devices.

The current scope runs Gateway as the production DICOM Storage SCP front door,
Orthanc as the internal storage/index/viewer backend, MWL as the dedicated
worklist SCP, and KaosPACS Web as a small past-study browser. Orthanc uses
PostgreSQL for metadata/index storage while DICOM binaries stay on host file
storage.

KaosPACS remains EMR-agnostic. eGHIS integration, polling, advanced routing,
broad charset fixing, and ViewRex database migration remain separate future
work.

## Architecture Stage

Current runtime:

- Gateway owns the legacy storage identity `VIEWREX:104`.
- Orthanc DICOM is internal only on `orthanc:11112` and is not published for
  direct modality traffic.
- MWL owns `VIEWREX_WL:105`, active worklist state, the local MWL API, and the
  minimal audit database.
- MWL expires active entries internally when `ExpiresAt` has passed, or when
  no `ExpiresAt` exists and the scheduled imaging date has passed. Expiry is a
  KaosPACS imaging lifecycle state, not a source cancellation.
- Gateway provides workflow API endpoints in front of the MWL API, including
  normalized order event endpoints for future KaosEghis-PACS integration and
  an operator-facing imaging worklist view for UI state. Gateway HTTP host
  publishing is deployment-configurable: same-host deployments may bind
  `127.0.0.1`, while cross-machine KaosEghis-PACS integration should bind
  `0.0.0.0` with bearer-token auth and firewall restriction.
- Gateway receives production C-STORE as `VIEWREX:104`, stores a local copy
  under `/app/data/dicom-inbox`, forwards the dataset to Orthanc, matches the
  study to active MWL entries, and completes the matched worklist item. It
  records a read-only charset/tag inspection summary at
  `/app/data/dicom_inspection.jsonl`. The guarded charset fixer is enabled by
  default and supports declared `ISO_IR 149` / `ISO 2022 IR 149`, plus the
  validated INNOVISION missing-charset EUC-KR display-text pattern, to
  `ISO_IR 192` for approved display text fields. It does not modify UIDs,
  pixel data, PatientID,
  AccessionNumber, Modality, private tags, or unapproved fields.
- Gateway can protect workflow endpoints with `GATEWAY_API_TOKEN` bearer-token
  authentication. `/health` remains unauthenticated.
- Gateway writes a minimal workflow audit DB at
  `/app/data/gateway_audit.sqlite3`, persisted under
  `/srv/docker/kaospacs/gateway`.
- Gateway writes separate operational modality metadata at
  `/app/data/gateway_operational_metadata.sqlite3` after a successful DICOM to
  MWL match. This is KaosPACS-owned display/routing metadata only. It keeps
  raw DICOM modality, workflow modality, station AET, study type, derived
  display modality, and AIO routing candidate outside Orthanc. It does not
  modify DICOM tags and does not write Orthanc metadata.
- Gateway includes a DICOM forwarding queue foundation at
  `/app/data/gateway_queue.sqlite3`. Direct forwarding is the default
  production path. Queue mode and the retry worker remain available for
  configured retry-based forwarding.
- Gateway is the single workflow and storage integration boundary.
- Orthanc is the internal storage, index, REST, DICOMweb, and viewer backend.
- KaosPACS Web is an Orthanc study browser, Weasis launcher, and
  patient-context document upload surface. It does not own MWL state, infer
  completion/expiry, receive modality DICOM, or change Gateway
  receive/forward/charset behavior. Web uploads write generated JPG/PNG/PDF-
  derived DICOM directly to Orthanc.
- KaosEghis-PACS will remain the EMR-aware adapter that reads eGHIS with
  read-only access, normalizes orders, and sends worklist events to Gateway.
  It should not call MWL directly in production, call Orthanc directly, or
  infer DICOM study completion.
- KaosEghis-PACS owns source order create/update/cancel/delete/restore events.
  KaosPACS owns imaging lifecycle completion and expiry only; it must not infer
  source cancellation or deletion by polling eGHIS or `public.mwl`.
- MWL remains the dedicated DICOM Modality Worklist SCP at `VIEWREX_WL:105`.
  Legacy modalities query MWL directly with C-FIND; Gateway does not own or
  proxy the MWL DICOM port.

## Legacy Identity

Production behavior must preserve the old PACS identity expected by legacy
modalities:

```text
PACS IP:          192.168.0.200
Storage SCP AET: VIEWREX
Storage SCP port: 104
MWL SCP AET:     VIEWREX_WL
MWL SCP port:    105
```

Do not casually rename these values in production configs. Local tests may use
temporary overrides, but production defaults are part of the compatibility
contract.

## Metadata Boundaries

- DICOM metadata is what the modality sent and Orthanc stores. Gateway charset
  normalization may fix approved Korean display text, but this task does not
  fill blank DICOM `Modality` tags.
- KaosPACS operational metadata is Gateway-owned SQLite state used for
  display/routing when workflow evidence is clearer than a blank DICOM tag.
  It is not written into Orthanc metadata.
- AIO reports are separate future/adjacent outputs. Operational metadata must
  not contain AI findings, diagnoses, or report text.

## Directory Layout

```text
kaospacs/
├── README.md
├── docker-compose.yml
├── .env.example
├── docs/
├── orthanc/
│   └── orthanc.json
├── postgres/
├── mwl/
├── gateway/
├── web/
└── migration/
    ├── README.md
    └── viewrex/
```

## Host Directory Setup

```bash
sudo mkdir -p /srv/projects
sudo mkdir -p /srv/docker/kaospacs/{orthanc-storage,postgres,logs,backups,mwl,gateway,web}
```

## First Run

```bash
cp .env.example .env
docker compose config
docker compose up -d
docker compose ps
```

## Test Endpoints

- Orthanc HTTP: `http://192.168.0.200:8042`
- KaosPACS Web: `http://192.168.0.200/emr.php`
- Gateway production DICOM SCP: `192.168.0.200:104`, AET `VIEWREX`
- Orthanc internal DICOM backend: `orthanc:11112`, AET `VIEWREX`
- MWL SCP: `192.168.0.200:105`, AET `VIEWREX_WL`
- MWL DICOM charset: `ISO 2022 IR 149` by default for legacy Korean BMD
  compatibility. JSON/API worklist data remains UTF-8.
- MWL local API: `http://127.0.0.1:8055/health`
- Gateway health: `http://127.0.0.1:8060/health`
- Gateway protected status: `http://127.0.0.1:8060/status`
- Gateway worklist API: `http://127.0.0.1:8060/worklist`
- Gateway imaging worklist API: `http://127.0.0.1:8060/imaging/worklist`
- Gateway operational metadata lookup:
  - `GET http://127.0.0.1:8060/imaging/operational-metadata/study/<orthancStudyId>`
  - `GET http://127.0.0.1:8060/imaging/operational-metadata/accession/<accessionNumber>`
- Gateway normalized order API:
  - `POST http://127.0.0.1:8060/orders/upsert`
  - `POST http://127.0.0.1:8060/orders/cancel`
- Gateway protected admin API:
  - `POST http://127.0.0.1:8060/admin/worklist/prune`
- KaosPACS Web is an Orthanc study browser, Weasis launcher, and
  patient-context document upload surface. It reads Orthanc over Docker
  internal HTTP and generates `weasis://` links that ask Weasis to load studies from
  `http://192.168.0.200:8042/dicom-web`. Workstations need Weasis installed
  and registered for the `weasis://` protocol. When eGHIS opens
  `/emr.php?m_patid=<chart_no>&m_patname=<name>&m_dob=<yyyymmdd>&m_sex=<M|F|O>`,
  Web scopes the page to that PatientID, displays chart number/name/DOB/sex
  from the launch context, and allows repeated pasted screenshots/images, JPG,
  PNG, or PDF upload directly into Orthanc as DICOM for that patient. Pasted
  images are queued before upload, can be removed or reordered with Move
  up/Move down, and each pasted image becomes a separate DICOM Secondary
  Capture object. PDF upload remains file-picker only. Pasted images can be
  uploaded without saving a temporary file on the desktop. V1 upload does not
  ask the operator to manually enter patient demographics.
- Gateway DICOM front door: enabled by default as `VIEWREX:104`. It stores
  received DICOM objects under `/app/data/dicom-inbox` and forwards them to
  Orthanc at `orthanc:11112`. It appends non-PHI charset/tag inspection summaries to
  `/app/data/dicom_inspection.jsonl` when
  `GATEWAY_DICOM_INSPECTION_ENABLED=true`. The charset fixer is enabled by
  default with `GATEWAY_DICOM_CHARSET_FIX_ENABLED=true` and
  `GATEWAY_DICOM_CHARSET_FIX_MODE=iso_ir_149_to_utf8`; reports are written to
  `/app/data/dicom_charset_fix.jsonl`. It applies only to declared Korean
  acquisition DICOM character sets or the validated missing-charset EUC-KR
  display-text pattern. Missing charset with plain ASCII text is still skipped;
  unknown charsets and UTF-8 are also skipped. When a fix applies, Gateway
  keeps the original received file in `/app/data/dicom-inbox` and writes the
  normalized forwarding copy under `/app/data/dicom-inbox/forwarded`. To disable, set
  `GATEWAY_DICOM_CHARSET_FIX_ENABLED=false` and
  `GATEWAY_DICOM_CHARSET_FIX_MODE=off`, then restart Gateway. `GATEWAY_DICOM_FORWARD_MODE=direct`
  is the default. Optional
  `GATEWAY_DICOM_FORWARD_MODE=queue` stores locally, enqueues, returns success
  after enqueue, and lets the retry worker forward later. Queue mode does not
  match or complete MWL worklists yet. Queue enqueueing is idempotent by
  `SOPInstanceUID`, so repeated modality sends do not create duplicate queue
  rows. In direct mode, when a received study is stored, forwarded, and matched
  to an active MWL entry with an accession number, Gateway calls MWL completion.
  Matching uses `AccessionNumber`, then `RequestedProcedureID`, then
  `ScheduledProcedureStepID`; it never uses patient name, DOB, or fuzzy matching.
  After a successful match, Gateway also saves operational modality metadata in
  its own SQLite DB. For example, a blank DICOM `Modality` from INNOVISION with
  workflow `Modality=CR` is displayed/routed as `X-ray` with AIO candidate
  `cxr`; `BMD` maps to `bmd`, `ECG` maps to `ecg`, and unknown values map to
  `unsupported`. This does not fill or overwrite the DICOM `Modality` tag.

If `GATEWAY_API_TOKEN` is set, Gateway workflow requests must include:

```text
Authorization: Bearer <token>
```

Generate a random value, for example with `openssl rand -hex 32`, and do not
commit the production token. Leaving `GATEWAY_API_TOKEN` empty disables Gateway
authentication for development only. This shared token is a simple clinic
LAN/localhost boundary for KaosEghis-PACS integration, not internet-grade
security.

Gateway HTTP host publishing is controlled by `GATEWAY_HTTP_BIND`. For
same-host deployments it may be `127.0.0.1`. For cross-machine KaosEghis-PACS
integration, set `GATEWAY_HTTP_BIND=0.0.0.0`, keep `GATEWAY_API_TOKEN` set, and
restrict access with the clinic firewall. The MWL HTTP API remains published on
host loopback only and must not be exposed on the LAN.

KaosPACS Web supports browser Basic Auth for operator access:

```text
WEB_AUTH_USERNAME=kaospacs
WEB_AUTH_PASSWORD=<random-password>
```

Set a random local password in `.env` and do not commit it. Leaving
`WEB_AUTH_PASSWORD` empty disables Web authentication for development only.
`GET /health` remains unauthenticated for Docker health checks.

`GET /status` is an operational endpoint and is protected when
`GATEWAY_API_TOKEN` is set. It reports dependency reachability and ownership
state only. It must not expose worklist entries, patient demographics, chart
numbers, accession numbers, diagnosis, EMR notes, tokens, or request payloads.
It also reports Gateway DICOM ownership, forwarding target, inspection report
path, charset-fix setting/report path, and queue state. Gateway DICOM queue
status is operational only and reports counts by queue state plus retry worker
enabled/running state; it does not expose patient demographics or dataset
contents.

`GET /imaging/worklist` is the operator-facing imaging lifecycle endpoint for
KaosEghis-PACS UI. It reads the current MWL JSON through Gateway, derives
imaging lifecycle state, and returns flat rows plus counts. By default it
returns only `active`, `completed`, `expired`, and `cancelled` rows.
`inactive` rows are included only when calling
`GET /imaging/worklist?view=all`; inactive means a retained non-actionable row
with no completion, expiry, or source cancellation timestamp. KaosEghis-PACS UI
must not treat inactive rows as active orders.

`POST /orders/upsert` accepts UTF-8 JSON normalized by KaosEghis-PACS,
preserves Korean text, and returns a stable response:

```json
{"status":"ok","action":"upserted","AccessionNumber":"..."}
```

`POST /orders/cancel` accepts `AccessionNumber` and records explicit
source/business cancellation. KaosPACS does not infer cancellation from missing
source rows.

KaosEghis-PACS UI should use `/imaging/worklist` instead of reading raw
`public.mwl`, eGHIS tables, or MWL internals. Lower-level `GET /worklist`
remains available for temporary compatibility, reconcile, and debug workflows.

`POST /admin/worklist/prune` removes old inactive completed, cancelled, or
expired entries from the runtime MWL worklist only. It defaults to
`dry_run=true` and the completed, cancelled, and expired statuses. It never
removes `Active=true` entries and returns a summary with accession numbers only.
It does not prune the MWL audit DB or Gateway audit DB.

Port `104` is a privileged low port. Binding it may require a rootful Docker
daemon, host networking, or adjusted capabilities depending on the environment.
Gateway owns this port. Orthanc DICOM is internal only and is not published for
direct modality traffic.

## MWL Runtime Data

The checked-in MWL seed file is mounted read-only at
`/app/config/worklist.json`. On first startup, the MWL service initializes the
active runtime worklist at `/app/data/worklist.json`.

`/app/data` persists on the host at `/srv/docker/kaospacs/mwl` and also stores
the minimal audit database at `/app/data/mwl_audit.sqlite3`.

MWL C-FIND returns only `Active=true` entries. Completed, expired, and
cancelled entries remain in the runtime JSON until pruned, but they are hidden
from modalities. Expired entries are marked with `ExpiredAt` and
`ExpireReason=expired_without_imaging`; cancelled entries are explicit
source/business cancellations and use `CancelledAt`.
