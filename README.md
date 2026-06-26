# KaosPACS

KaosPACS is a Docker-based PACS replacement stack for an expired proprietary
ViewRex PACS system used with eGHIS EMR and legacy imaging devices.

The current initial scope is deliberately small: run Orthanc with PostgreSQL
metadata/index storage while keeping DICOM binaries on host file storage.

Future services such as MWL, eGHIS polling, gateway logic, web launch, Weasis
launching, charset evaluation, and ViewRex database migration are documented
but not implemented yet.

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
- DICOM SCP: `192.168.0.200:104`, AET `VIEWREX`

Port `104` is a privileged low port. Binding it may require a rootful Docker
daemon, host networking, or adjusted capabilities depending on the environment.
