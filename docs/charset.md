# DICOM Charset

Korean text has two separate paths in KaosPACS:

- Order/worklist text created through Gateway and served by MWL.
- Modality-produced acquisition DICOM stored by Orthanc.

Keep those paths separate. Worklist text can be UTF-8-safe today. Acquisition
DICOM charset rewriting must stay guarded and opt-in until validated with real
clinical modality samples.

## Worklist Text

Gateway and MWL read and write JSON as UTF-8. Gateway order APIs preserve Korean
patient and study text when converting normalized order events into MWL entries.
MWL writes runtime JSON with UTF-8 text preserved and returns JSON responses as:

```text
application/json; charset=utf-8
```

MWL DICOM responses default to:

```text
SpecificCharacterSet = ISO_IR 192
```

`ISO_IR 192` is DICOM UTF-8. It is used unless a worklist entry explicitly
provides another `SpecificCharacterSet`.

## Acquisition DICOM

`SpecificCharacterSet=ISO_IR 149` has been observed in Korean modality-created
DICOM data. Orthanc can receive and store DICOM data, but viewer behavior and
Korean text display still need evaluation with actual clinical samples and
target viewers.

## Current Rule

Default runtime remains inspection-only:

```text
GATEWAY_DICOM_CHARSET_FIX_ENABLED=false
GATEWAY_DICOM_CHARSET_FIX_MODE=off
```

Do not enable charset fixing until real Korean modality samples have been
validated in Orthanc Explorer, Stone Viewer, Weasis, and raw DICOM inspection.
Rollback is:

```text
GATEWAY_DICOM_CHARSET_FIX_ENABLED=false
docker compose up -d gateway
```

## Handling Point

Korean charset and tag inspection belongs at the Gateway ingestion point.
Gateway records read-only inspection summaries for received DICOM objects. It
also has a conservative opt-in fixer for one validated path:

```text
GATEWAY_DICOM_CHARSET_FIX_ENABLED=true
GATEWAY_DICOM_CHARSET_FIX_MODE=iso_ir_149_to_utf8
```

Do not put charset normalization inside MWL or KaosEghis-PACS. Do not rely on
Orthanc as the long-term place for modality-facing charset fixes; Orthanc
should remain the internal storage/index/viewer backend.

With the fixer disabled, the Gateway C-STORE front door stores and forwards
datasets unchanged. It inspects charset/tag shape only and appends non-PHI
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
the guarded fixer is explicitly enabled.

## Opt-In ISO_IR 149 Fixer

The only supported fixer mode is:

```text
iso_ir_149_to_utf8
```

When enabled, Gateway processes only datasets whose `SpecificCharacterSet`
contains `ISO_IR 149`. It writes the original received file under
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
