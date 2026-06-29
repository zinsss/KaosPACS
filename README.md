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
- Gateway provides localhost-only workflow API endpoints in front of the MWL
  API, including normalized order event endpoints for future KaosEghis-PACS
  integration. It does not own DICOM ports, receive studies, or forward to
  Orthanc yet.
- Gateway can protect workflow endpoints with `GATEWAY_API_TOKEN` bearer-token
  authentication. `/health` remains unauthenticated.
- Gateway writes a minimal workflow audit DB at
  `/app/data/gateway_audit.sqlite3`, persisted under
  `/srv/docker/kaospacs/gateway`.
- This keeps the verified Orthanc + MWL storage path stable while Gateway DICOM
  behavior is still unimplemented.

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
