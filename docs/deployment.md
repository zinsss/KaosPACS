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
- `PACS_HOST_IP=192.168.0.200`

## Startup

```bash
docker compose config
docker compose up -d
docker compose ps
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
- KaosPACS configuration and operational logs.
