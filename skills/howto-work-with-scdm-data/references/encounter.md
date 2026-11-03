# Encounter

SCDM 8.2.2 · TabID ENC

One record per patient encounter; the parent record for diagnoses and procedures.

Each encounter has a single record, keyed by `PatID` and `EncounterID`. Each diagnosis and procedure recorded during the encounter gets its own record in the Diagnosis or Procedure tables. Process rollback transactions and other adjustments before populating this table; how Data Partners handle this varies and may be affected by billing cycles.

- Conversion from Ambulatory or Emergency Visit (`ED`, `AV`, or `OA`) into Inpatient Hospital or Institutional Stay (`IP`, `IS`) through admission represents multiple encounters: the ambulatory or emergency visit is one encounter, the inpatient or institutional stay after admission is a second encounter.
- Transfer from one facility to another starts a new encounter at the new facility.
- If supported by source data, multiple visits on the same day to the same facility — with no conversion from `ED`, `AV`, or `OA` into `IP` or `IS` through admission — should be created as distinct encounters, including all diagnoses and procedures recorded during those separate visits, with distinct `ProviderID` values populated. If source data cannot distinguish same-day visits, populate as a single encounter.

Encounters for a patient to the same facility but with different individual providers should be distinct encounters. Multiple rows may share the same `PatID` / `ADate` / `FacilityID` / `EncType` while still satisfying the unique-row rule; uniqueness of encounters can be verified by linking to the `ProviderID` values in the Diagnosis and Procedure tables. For claims-based source systems:

- Multiple claims that reflect a single encounter should be rolled into one encounter — for example, an inpatient hospitalization that generates multiple facility claims.
- Transfers from one hospitalization to another should have each facility's hospitalization set as a single encounter.
- Claims with visits on multiple dates should have each date established as a separate encounter, such as multiple outpatient visits to the same provider billed on a single claim.

**Sort:** `PatID`, then `ADate`.

**Unique row:** `PatID`, `EncounterID`.

> Caveat: the table description identifies each record by `PatID` and `EncounterID`, while the unique-row rule names `EncounterID` alone. This tension is present in the source documentation.

**Numeric lengths:** `Num(#)` variables take an as-needed length — use the fewest bytes that hold all distinct values. A numeric variable of a given byte length stores integers exactly up to: 3 bytes → 8,192; 4 → 2,097,152; 5 → 536,870,912; 6 → 137,438,953,472; 7 → 35,184,372,088,832; 8 → 9,007,199,254,740,992.

## Variables

### `PatID`
**Num(#)** (as-needed length) · Missing: never. Example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables.

`PatID` is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.

### `EncounterID`
**Num(#)** (as-needed length) · Missing: never. Example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, Prescribing, and Patient-Reported Measures Response tables.

For efficiency, medical utilization data is captured in three tables: **Encounter** (the record characterizing the outpatient visit or hospital stay), **Diagnosis** (the diagnosis or other clinical codes associated with the encounter), and **Procedure** (the procedure codes associated with the encounter). All diagnoses and procedures for an encounter should share the same `EncounterID`. "Orphan" diagnosis or procedure records whose `EncounterID` has no match in the Encounter table are allowable. The Encounter, Diagnosis, Procedure, Inpatient Transfusion, Inpatient Pharmacy, Vital Signs, Mother-Infant Linkage, and Prescribing tables are linked by `PatID` and `EncounterID`.

### `ADate`
**Num(4)** · Missing: never. Example: `12/24/2019`

Encounter or admission date. Values are SAS dates.

### `DDate`
**Num(4)** · Missing: conditional on `EncType`. Example: `12/31/2019`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Discharge date. Values: SAS Date; `.S` (special missing); `.` (missing).

Populate for all Inpatient Hospital Stay (`IP`) and Non-Acute Institutional Stay (`IS`) encounter types. May be populated for Emergency Department (`ED`) encounters. Should be missing for ambulatory visit (`AV` or `OA`) encounters. If `EncType` is `IP` or `IS`, the only missing value allowed is the special missing `.S` — actual discharge dates remain valid; use `.S` for patients still in the facility.

### `EncType`
**Char(2)** · Missing: never. Example: `IP`

Encounter Type. Values:

- `AV` — Ambulatory Visit (includes visits at outpatient clinics, same day surgeries, urgent care visits, and other same-day ambulatory hospital encounters, but excludes Emergency Department encounters. Transfer from an AV facility to an ED facility starts a new encounter at the new facility.)
- `ED` — Emergency Department (includes ED encounters that become inpatient stays through hospital admission: the ED is one encounter, the Inpatient Hospital Stay after admission from the ED is a second encounter. ED data should be identified before hospitalization data to ensure that ED with subsequent admission won't be rolled up into the hospital event. Transfer from one ED facility to another ED facility starts a new encounter at the new facility. Excludes urgent care visits.)
- `IP` — Inpatient Hospital Stay (includes all inpatient stays, same-day hospital discharges, and other acute hospital care where discharge is after the admission date. Transfer from one facility to another starts a new encounter at the new facility.)
- `IS` — Non-Acute Institutional Stay (includes hospice, skilled nursing facility (SNF), rehab center, nursing home, residential, overnight non-hospital dialysis, and other non-hospital stays. Transfer from one facility to another starts a new encounter at the new facility.)
- `OA` — Other Ambulatory Visit (includes other non-overnight `AV` encounters such as hospice visits, home health visits, skilled nursing facility visits, other non-hospital visits, as well as telemedicine, telephone, and email consultations.)

### `FacilityID`
**Num(#)** (as-needed length) · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `45678`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Servicing provider identifier. Local facility pseudo-identifier for the hospital or clinic where the encounter occurred; there must be only one facility per encounter. Used for chart abstraction and validation, and links to the Facility table. If unavailable, set to `.U`.

If all `FacilityID` values are unknown (set to `.U`), use `Num(3)`. If the `FacilityID` length is greater than 3 in any other table, set the length to the maximum length found across all tables. Variable lengths must be consistent across tables for same-named variables.

### `Discharge_Disposition`
**Char(1)** · Missing: conditional on `EncType`. Example: `A`

Values:

- `A` — Discharged alive
- `E` — Expired
- `S` — Still in facility
- `U` — Unknown

Populate for Inpatient Hospital Stay (`IP`) and Non-Acute Institutional Stay (`IS`) encounter types. May be populated for Emergency Department (`ED`) encounters. Should be missing for ambulatory visit (`AV` or `OA`) encounters.

### `Discharge_Status`
**Char(2)** · Missing: conditional on `EncType`. Example: `SN`

Values:

- `AF` — Adult Foster Home
- `AL` — Assisted Living Facility
- `AM` — Against Medical Advice
- `AW` — Absent without leave
- `EX` — Expired
- `HH` — Home Health
- `HO` — Home / Self Care
- `HS` — Hospice
- `IP` — Other Acute Inpatient Hospital
- `NH` — Nursing Home (Includes Intermediate Care Facility (ICF))
- `OT` — Other
- `RH` — Rehabilitation Facility
- `RS` — Residential Facility
- `SH` — Still In Facility
- `SN` — Skilled Nursing Facility
- `UN` — Unknown

Populate for Inpatient Hospital Stay (`IP`) and Non-Acute Institutional Stay (`IS`) encounter types. May be populated for Emergency Department (`ED`) encounters. Should be missing for ambulatory visit (`AV` or `OA`) encounters.

### `DRG`
**Char(3)** · Missing: conditional on `EncType`. Example: `372`

Diagnosis Related Group — a 3-digit code. Use leading zeroes for codes less than 100.

Populate for `IP` and `IS` encounter types. May be populated for `ED` encounters. Can be missing for `IP`, `IS`, or `ED`. Should be missing for `AV` or `OA` encounters.

### `DRG_Type`
**Char(1)** · Missing: conditional on `EncType`. Example: `1`

DRG code version. MS-DRG (the current system) began on 10/1/2007. Should be missing for `AV` or `OA` encounters. Can be missing for `IP`, `IS`, or `ED`.

Values:

- `1` — CMS-DRG (old system)
- `2` — MS-DRG (current system)

### `Admitting_Source`
**Char(2)** · Missing: conditional on `EncType`. Example: `HH`

Values:

- `AF` — Adult Foster Home
- `AL` — Assisted Living Facility
- `AV` — Ambulatory Visit
- `ED` — Emergency Department
- `HH` — Home Health
- `HO` — Home / Self Care
- `HS` — Hospice
- `IP` — Other Acute Inpatient Hospital
- `NH` — Nursing Home (Includes ICF)
- `OT` — Other
- `RH` — Rehabilitation Facility
- `RS` — Residential Facility
- `SN` — Skilled Nursing Facility
- `UN` — Unknown

Populate for Inpatient Hospital Stay (`IP`) and Non-Acute Institutional Stay (`IS`) encounter types. May be populated for Emergency Department (`ED`) encounters. Should be missing for ambulatory visit (`AV` or `OA`) encounters.
