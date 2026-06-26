#!/usr/bin/env python3
"""Query the local KaosPACS MWL SCP and print returned worklist fields."""

from __future__ import annotations

import sys

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pynetdicom import AE
from pynetdicom.sop_class import ModalityWorklistInformationFind


HOST = "127.0.0.1"
PORT = 105
CALLING_AE = "KAOSPACS_TEST"
CALLED_AE = "VIEWREX_WL"


def build_query() -> Dataset:
    query = Dataset()
    query.QueryRetrieveLevel = "WORKLIST"
    query.PatientName = ""
    query.PatientID = ""
    query.PatientBirthDate = ""
    query.PatientSex = ""
    query.AccessionNumber = ""
    query.RequestedProcedureDescription = ""

    step = Dataset()
    step.Modality = "BMD"
    step.ScheduledStationAETitle = "BMD"
    step.ScheduledProcedureStepStartDate = ""
    step.ScheduledProcedureStepStartTime = ""
    step.ScheduledProcedureStepDescription = ""
    query.ScheduledProcedureStepSequence = Sequence([step])
    return query


def value(dataset: Dataset, keyword: str) -> str:
    raw = getattr(dataset, keyword, "")
    if raw is None:
        return ""
    return str(raw)


def print_match(identifier: Dataset) -> None:
    step = identifier.ScheduledProcedureStepSequence[0]
    print("PatientID:", value(identifier, "PatientID"))
    print("PatientName:", value(identifier, "PatientName"))
    print("AccessionNumber:", value(identifier, "AccessionNumber"))
    print("Modality:", value(step, "Modality"))
    print("ScheduledStationAETitle:", value(step, "ScheduledStationAETitle"))
    print(
        "ScheduledProcedureStepDescription:",
        value(step, "ScheduledProcedureStepDescription"),
    )


def main() -> int:
    ae = AE(ae_title=CALLING_AE)
    ae.add_requested_context(ModalityWorklistInformationFind)

    assoc = ae.associate(HOST, PORT, ae_title=CALLED_AE)
    if not assoc.is_established:
        print(f"Association failed: {CALLED_AE}@{HOST}:{PORT}", file=sys.stderr)
        return 1

    matches = 0
    try:
        for status, identifier in assoc.send_c_find(build_query(), ModalityWorklistInformationFind):
            if status is None:
                print("C-FIND failed: no status returned", file=sys.stderr)
                return 1

            print(f"C-FIND status: 0x{status.Status:04X}")
            if identifier is None:
                continue

            matches += 1
            print_match(identifier)
    finally:
        assoc.release()

    print("Matches:", matches)
    return 0 if matches else 2


if __name__ == "__main__":
    raise SystemExit(main())
