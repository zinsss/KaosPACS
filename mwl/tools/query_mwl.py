#!/usr/bin/env python3
"""Query the local KaosPACS MWL SCP and print returned worklist fields."""

from __future__ import annotations

import sys
import argparse

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pynetdicom import AE
from pynetdicom.sop_class import ModalityWorklistInformationFind


HOST = "127.0.0.1"
PORT = 105
CALLING_AE = "KAOSPACS_TEST"
CALLED_AE = "VIEWREX_WL"


def build_query(
    patient_id: str,
    accession_number: str,
    modality: str,
    station_aet: str,
) -> Dataset:
    query = Dataset()
    query.QueryRetrieveLevel = "WORKLIST"
    query.PatientName = ""
    query.PatientID = patient_id
    query.PatientBirthDate = ""
    query.PatientSex = ""
    query.AccessionNumber = accession_number
    query.RequestedProcedureDescription = ""

    step = Dataset()
    step.Modality = modality
    step.ScheduledStationAETitle = station_aet
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
    print("---")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", default=PORT, type=int)
    parser.add_argument("--calling-ae", default=CALLING_AE)
    parser.add_argument("--called-ae", default=CALLED_AE)
    parser.add_argument("--patient-id", default="")
    parser.add_argument("--accession-number", default="")
    parser.add_argument("--modality", default="")
    parser.add_argument("--station-aet", default="")
    args = parser.parse_args()

    ae = AE(ae_title=args.calling_ae)
    ae.add_requested_context(ModalityWorklistInformationFind)

    assoc = ae.associate(args.host, args.port, ae_title=args.called_ae)
    if not assoc.is_established:
        print(f"Association failed: {args.called_ae}@{args.host}:{args.port}", file=sys.stderr)
        return 1

    matches = 0
    try:
        query = build_query(
            patient_id=args.patient_id,
            accession_number=args.accession_number,
            modality=args.modality,
            station_aet=args.station_aet,
        )
        for status, identifier in assoc.send_c_find(query, ModalityWorklistInformationFind):
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
