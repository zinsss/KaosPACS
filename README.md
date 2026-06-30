# KaosPACS

KaosPACS is a Docker-based PACS replacement stack for an expired proprietary
ViewRex PACS system used with eGHIS EMR and legacy imaging devices.

The current scope runs Orthanc with PostgreSQL metadata/index storage while
keeping DICOM binaries on host file storage. It also includes a KaosPACS MWL
service with a localhost-only update API and a minimal SQLite audit database.

KaosPACS remains EMR-agnostic. eGHIS integration, polling, routing, web launch,
Weasis launch coordination, charset evaluation, and ViewRex database migration
remain separate future work.

## Architecture Stage

Current transitional runtime:

- Orthanc temporarily owns the legacy storage identity `VIEWREX:104`.
- MWL owns `VIEWREX_WL:105`, active worklist state, the local MWL API, and the
  minimal audit database.
- MWL expires active entries internally when `ExpiresAt` has passed, or when
  no `ExpiresAt` exists and the scheduled imaging date has passed. Expiry is a
  KaosPACS imaging lifecycle state, not a source cancellation.
- Gateway provides localhost-only workflow API endpoints in front of the MWL
  API, including normalized order event endpoints for future KaosEghis-PACS
  integration.
- Gateway includes a disabled DICOM C-STORE skeleton for loopback testing only.
  It does not bind port `104`, does not use AET `VIEWREX`, does not receive
  production studies, and does not forward to Orthanc unless explicit
  test-mode forwarding is enabled. The default forwarding mode is `direct`,
  where Gateway can match the received study to the active MWL worklist after
  successful local receipt and optional direct forwarding, then complete the
  matched worklist item. Optional `queue` mode is test-mode only and does not
  match or complete worklists yet.
- Gateway can protect workflow endpoints with `GATEWAY_API_TOKEN` bearer-token
  authentication. `/health` remains unauthenticated.
- Gateway writes a minimal workflow audit DB at
  `/app/data/gateway_audit.sqlite3`, persisted under
  `/srv/docker/kaospacs/gateway`.
- Gateway includes a disabled DICOM forwarding queue foundation at
  `/app/data/gateway_queue.sqlite3`. Queue enqueueing and the retry worker are
  both disabled by default; current test-mode direct forwarding remains
  unchanged.
- This keeps the verified Orthanc + MWL storage path stable while Gateway DICOM
  behavior remains non-production test scaffolding.

Final Gateway-centered runtime:

- Gateway will become the modality-facing DICOM Storage SCP at
  `192.168.0.200:104`, AET `VIEWREX`.
- Gateway will become the single workflow and storage integration boundary.
- Orthanc will move behind Gateway as the internal storage, index, REST,
  DICOMweb, and viewer backend.
- Gateway will receive studies from modalities, inspect or fix Korean
  charset/tag issues only after safe validation, forward studies to Orthanc,
  and then call `POST /worklist/complete` after successful receive/forward.
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
- Current transitional DICOM SCP: `192.168.0.200:104`, AET `VIEWREX`
- MWL SCP: `192.168.0.200:105`, AET `VIEWREX_WL`
- MWL local API: `http://127.0.0.1:8055/health`
- Gateway health: `http://127.0.0.1:8060/health`
- Gateway protected status: `http://127.0.0.1:8060/status`
- Gateway worklist API: `http://127.0.0.1:8060/worklist`
- Gateway normalized order API:
  - `POST http://127.0.0.1:8060/orders/upsert`
  - `POST http://127.0.0.1:8060/orders/cancel`
- Gateway protected admin API:
  - `POST http://127.0.0.1:8060/admin/worklist/prune`
- Gateway DICOM skeleton: disabled by default. If explicitly enabled for local
  tests, it uses `127.0.0.1:11104`, AET `KAOSPACS_GW_TEST`, and stores files
  under `/app/data/dicom-inbox`. It is not the production `VIEWREX:104`
  receiver. Test-mode forwarding to Orthanc is also disabled by default with
  `GATEWAY_DICOM_FORWARD_ENABLED=false`. The persistent queue foundation is
  also disabled by default with `GATEWAY_DICOM_QUEUE_ENABLED=false`; when
  enabled, successful local stores insert pending queue rows. The retry worker
  is separately disabled by default with `GATEWAY_QUEUE_WORKER_ENABLED=false`;
  when explicitly enabled, it forwards queued files to Orthanc and updates
  queue state. `GATEWAY_DICOM_FORWARD_MODE=direct` is the default and preserves
  the current direct-forwarding path. `GATEWAY_DICOM_FORWARD_MODE=queue` is
  test-mode only: C-STORE stores locally, enqueues, returns success after
  enqueue, and the worker forwards later. Queue mode does not match or complete
  MWL worklists yet. Queue enqueueing is idempotent by `SOPInstanceUID`, so
  repeated modality sends do not create duplicate queue rows. In direct mode,
  when a received test study is stored, optionally forwarded, and matched to an
  active MWL entry with an accession number, Gateway calls MWL completion. It
  does not perform charset fixes.
  Matching uses `AccessionNumber`, then `RequestedProcedureID`, then
  `ScheduledProcedureStepID`; it never uses patient name, DOB, or fuzzy matching.

If `GATEWAY_API_TOKEN` is set, Gateway workflow requests must include:

```text
Authorization: Bearer <token>
```

Generate a random value, for example with `openssl rand -hex 32`, and do not
commit the production token. Leaving `GATEWAY_API_TOKEN` empty disables Gateway
authentication for development only. This shared token is a simple clinic
LAN/localhost boundary for KaosEghis-PACS integration, not internet-grade
security.

`GET /status` is an operational endpoint and is protected when
`GATEWAY_API_TOKEN` is set. It reports dependency reachability and ownership
state only. It must not expose worklist entries, patient demographics, chart
numbers, accession numbers, diagnosis, EMR notes, tokens, or request payloads.
It also reports whether the disabled Gateway DICOM skeleton is enabled.
Gateway DICOM queue status is operational only and reports counts by queue
state plus retry worker enabled/running state; it does not expose patient
demographics or dataset contents.

`POST /admin/worklist/prune` removes old inactive completed, cancelled, or
expired entries from the runtime MWL worklist only. It defaults to
`dry_run=true` and the completed, cancelled, and expired statuses. It never
removes `Active=true` entries and returns a summary with accession numbers only.
It does not prune the MWL audit DB or Gateway audit DB.

Port `104` is a privileged low port. Binding it may require a rootful Docker
daemon, host networking, or adjusted capabilities depending on the environment.
In the final architecture, Gateway will own this port and Orthanc will no
longer be the modality-facing Storage SCP.

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
