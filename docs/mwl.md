# Modality Worklist

KaosPACS MWL is a small DICOM Modality Worklist SCP for legacy modality
testing. The first milestone is intentionally hardcoded and does not connect to
eGHIS.

## Fixed Production Identity

```text
AET:  VIEWREX_WL
Port: 105
```

Legacy modalities expect this identity. Production behavior must preserve it.

## Current Milestone

The current implementation returns one fictional BMD test worklist item:

```text
PatientName: TEST^BMD
PatientID: KAOSMWL001
AccessionNumber: KAOSMWL001
Modality: BMD
ScheduledStationAETitle: BMD
ScheduledProcedureStepDescription: BMD TEST
```

The service supports:

- Verification C-ECHO
- Modality Worklist C-FIND
- Basic query matching by PatientID, AccessionNumber, Modality, and
  ScheduledStationAETitle
- C-FIND summary logging for BMD test sessions

Run it with:

```bash
docker compose up -d mwl
docker logs --tail=100 kaospacs-mwl
```

Query it from inside the container:

```bash
docker compose exec mwl python tools/query_mwl.py
```

The utility queries `127.0.0.1:105` with calling AE `KAOSPACS_TEST` and called
AE `VIEWREX_WL`, then prints the returned patient and scheduled step fields.

The worklist item is configured in:

```text
mwl/config/worklist.json
```

## Later Milestone

Later MWL should derive worklist entries from eGHIS orders using read-only
database access or a read-only upstream feed. eGHIS polling must never mutate
the eGHIS database.

Do not add eGHIS polling, route logic, or Orthanc-side behavior in this
milestone.
