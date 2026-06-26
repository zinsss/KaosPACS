# DICOM Charset

Korean DICOM charset behavior is an evaluation issue, not part of initial stack
implementation.

## Observed

`SpecificCharacterSet=ISO_IR 149` has been observed in Korean DICOM data.

Orthanc can receive and store DICOM data. Viewer behavior and Korean text
display must be evaluated with actual clinical samples and target viewers.

## Initial Rule

Do not rewrite, normalize, or mass-fix DICOM character sets during initial
Orthanc/PostgreSQL setup.

Future charset work should document:

- Source modality behavior.
- Orthanc stored tags.
- Orthanc Explorer / Stone Viewer display.
- Weasis display.
- Any proposed normalization point and rollback strategy.
