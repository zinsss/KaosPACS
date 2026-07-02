# DICOM Charset

Korean text has two separate paths in KaosPACS:

- Order/worklist text created through Gateway and served by MWL.
- Modality-produced acquisition DICOM stored by Orthanc.

Keep those paths separate. Worklist text can be UTF-8-safe today. Acquisition
DICOM charset rewriting must stay guarded and limited to validated Korean
paths. The fixer is enabled by default so Gateway normalizes known legacy
Korean studies before Orthanc storage, and it can be explicitly disabled for
rollback.

## Worklist Text

Gateway and MWL read and write JSON as UTF-8. Gateway order APIs preserve Korean
patient and study text when converting normalized order events into MWL entries.
MWL writes runtime JSON with UTF-8 text preserved and returns JSON responses as:

```text
application/json; charset=utf-8
```

MWL DICOM C-FIND responses default to the legacy Korean character set expected
by the BMD workflow:

```text
SpecificCharacterSet = ISO 2022 IR 149
```

The runtime JSON remains UTF-8; the MWL service converts the outgoing DICOM
response according to `MWL_DICOM_CHARACTER_SET`. Use `ISO_IR 192` only if a
modality is verified to support UTF-8 MWL correctly.

## Acquisition DICOM

`SpecificCharacterSet=ISO_IR 149` has been observed in Korean modality-created
DICOM data. INNOVISION has also produced acquisition DICOM with missing
`SpecificCharacterSet` while supported display text fields contain EUC-KR bytes
that pydicom/Orthanc otherwise expose as Latin-1-looking mojibake. Orthanc can
receive and store DICOM data, but viewer behavior and Korean text display still
need evaluation with actual clinical samples and target viewers.

## Current Rule

The guarded fixer is enabled by default:

```text
GATEWAY_DICOM_CHARSET_FIX_ENABLED=true
GATEWAY_DICOM_CHARSET_FIX_MODE=iso_ir_149_to_utf8
```

It remains narrow. It processes:

- datasets whose `SpecificCharacterSet` contains `ISO_IR 149` or
  `ISO 2022 IR 149`
- datasets with missing `SpecificCharacterSet` only when approved display text
  fields match the validated EUC-KR mojibake pattern seen from INNOVISION

It skips missing charset with ASCII/no Korean-like text, unknown charset, and
`ISO_IR 192`. It does not perform broad guessing.

Rollback is:

```text
GATEWAY_DICOM_CHARSET_FIX_ENABLED=false
GATEWAY_DICOM_CHARSET_FIX_MODE=off
docker compose up -d gateway
```

## Handling Point

Korean charset and tag inspection belongs at the Gateway ingestion point.
Gateway records read-only inspection summaries for received DICOM objects. It
also has a conservative fixer for one validated path:

```text
GATEWAY_DICOM_CHARSET_FIX_ENABLED=true
GATEWAY_DICOM_CHARSET_FIX_MODE=iso_ir_149_to_utf8
```

Do not put charset normalization inside MWL or KaosEghis-PACS. Do not rely on
Orthanc as the long-term place for modality-facing charset fixes; Orthanc
should remain the internal storage/index/viewer backend.

When the fixer is disabled, the Gateway C-STORE front door stores and forwards
datasets unchanged. It still inspects charset/tag shape and appends non-PHI
JSONL reports to:

```text
/app/data/dicom_inspection.jsonl
```

The report includes identifiers already allowed in Gateway operational logs
such as SOP Instance UID, Study Instance UID, Series Instance UID, accession
number, modality, transfer syntax, declared `SpecificCharacterSet`, text tag
presence booleans, text VR counts, and charset review reasons. It does not
store PatientName values, PatientID values, DOB, sex, phone, address,
diagnosis, physician names, institution names, full datasets, or pixel data.

Gateway does not normalize or rewrite Korean acquisition character sets unless
the declared charset matches the guarded fixer rule, or the dataset matches the
validated missing-charset EUC-KR display-text pattern.

## Guarded Korean Fixer

The only supported fixer mode is:

```text
iso_ir_149_to_utf8
```

Gateway processes only datasets whose `SpecificCharacterSet` contains
`ISO_IR 149` or `ISO 2022 IR 149`, or datasets with missing
`SpecificCharacterSet` that contain the validated EUC-KR mojibake pattern in
approved display text fields. It writes the original received file under
`/app/data/dicom-inbox`, writes a normalized forwarding copy under
`/app/data/dicom-inbox/forwarded`, sets the forwarding copy
`SpecificCharacterSet` to `ISO_IR 192`, and forwards that copy to Orthanc.
Queue mode enqueues the normalized copy when a fix applies.

The fixer is intentionally conservative. It may rewrite only these display
text fields:

- `PatientName`
- `StudyDescription`
- `SeriesDescription`
- `RequestedProcedureDescription`
- `ScheduledProcedureStepDescription`
- `InstitutionName`
- `ReferringPhysicianName`
- `PerformingPhysicianName`

It also supports those fields inside nested sequence items, including
`ScheduledProcedureStepSequence`.

The fixer must not rewrite:

- `PatientID`
- `AccessionNumber`
- `Modality`
- UIDs
- pixel data
- private tags
- unknown text tags

Fix reports are non-PHI JSONL records at:

```text
/app/data/dicom_charset_fix.jsonl
```

They contain SOP/Study/Series UIDs, accession number, modality, original and
new character set, fix mode, whether a fix was applied, fixed keyword names,
skipped keyword names, and reason/error code. They must not contain old or new
text values, patient names, patient IDs, physician names, institution names,
full datasets, or pixel data.

Future charset work should document:

- Source modality behavior.
- Orthanc stored tags.
- Orthanc Explorer / Stone Viewer display.
- Weasis display.
- Gateway ingress behavior.
- Any proposed normalization point and rollback strategy.
