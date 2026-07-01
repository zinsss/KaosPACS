# DICOM Charset

Korean text has two separate paths in KaosPACS:

- Order/worklist text created through Gateway and served by MWL.
- Modality-produced acquisition DICOM stored by Orthanc.

Keep those paths separate. Worklist text can be UTF-8-safe today; acquisition
DICOM charset rewriting still needs clinical sample validation before any
production transformation.

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

Do not rewrite, normalize, or mass-fix acquisition DICOM character sets during
the current Orthanc/PostgreSQL runtime.

## Final Handling Point

In the final Gateway-centered architecture, Korean charset and tag inspection
belongs at the Gateway ingestion point. Gateway may fix charset/tag issues only
after safe validation with real samples, viewer checks, and a rollback plan.

Do not put charset normalization inside MWL or KaosEghis-PACS. Do not rely on
Orthanc as the long-term place for modality-facing charset fixes; Orthanc
should remain the internal storage/index/viewer backend.

The current Gateway C-STORE skeleton is disabled test scaffolding only. It
stores explicitly tested datasets when enabled but does not inspect, normalize,
or rewrite Korean acquisition character sets.

Future charset work should document:

- Source modality behavior.
- Orthanc stored tags.
- Orthanc Explorer / Stone Viewer display.
- Weasis display.
- Gateway ingress behavior.
- Any proposed normalization point and rollback strategy.
