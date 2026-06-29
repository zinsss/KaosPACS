# DICOM Charset

Korean DICOM charset behavior is an evaluation issue, not part of initial stack
implementation.

## Observed

`SpecificCharacterSet=ISO_IR 149` has been observed in Korean DICOM data.

Orthanc can receive and store DICOM data. Viewer behavior and Korean text
display must be evaluated with actual clinical samples and target viewers.

## Current Rule

Do not rewrite, normalize, or mass-fix DICOM character sets during initial
Orthanc/PostgreSQL setup.

## Final Handling Point

In the final Gateway-centered architecture, Korean charset and tag inspection
belongs at the Gateway ingestion point. Gateway may fix charset/tag issues only
after safe validation with real samples, viewer checks, and a rollback plan.

Do not put charset normalization inside MWL or KaosEghis-PACS. Do not rely on
Orthanc as the long-term place for modality-facing charset fixes; Orthanc
should remain the internal storage/index/viewer backend.

The current Gateway C-STORE skeleton is disabled test scaffolding only. It
stores explicitly tested datasets when enabled but does not inspect, normalize,
or rewrite Korean character sets.

Future charset work should document:

- Source modality behavior.
- Orthanc stored tags.
- Orthanc Explorer / Stone Viewer display.
- Weasis display.
- Gateway ingress behavior.
- Any proposed normalization point and rollback strategy.
