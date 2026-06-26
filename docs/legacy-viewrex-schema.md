# Legacy ViewRex Schema

Confirmed tables and fields of interest.

## QueueRecord

- `PatientID`
- `AccessNo`
- `PACSOCSBridgeKey`
- `Modality`
- `Status`
- `StudyCode`
- `StudyName`
- `Department`
- `OrderDoctor`
- `OCSComment`
- `TimeDate`

## StudyInformation

- `QueueRecordID`
- `PACSOCSBridgeKey`
- `StudyInstanceUID`
- `Modality`

## PACSGate

- `QueueRecordID`
- `StudyInfoID`
- `PACSOCSBridgeKey`
- `ReportText`
- `Finding`
- `Conclusion`
