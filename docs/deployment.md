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

Production defaults preserve:

- `ORTHANC_AET=VIEWREX`
- `ORTHANC_DICOM_PORT=104`
- `MWL_AET=VIEWREX_WL`
- `MWL_PORT=105`
- `MWL_API_PORT=8055`
- `PACS_HOST_IP=192.168.0.200`

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
docker compose logs mwl
```

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
- KaosPACS configuration and operational logs.
