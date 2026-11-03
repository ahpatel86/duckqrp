# Procedure

SCDM 8.2.2 · TabID PRO

Uniquely recorded procedures for all encounters, keyed by patient, encounter, code type, code, and provider.

The table contains one record per unique combination of `PatID`, `EncounterID`, code type, `PX`, and `ProviderID`. It should capture all uniquely recorded procedures for all encounters, per provider.

**Sort:** `PatID`, then `ADate`.

**Unique row:** `EncounterID`, `PX_CodeType`, `PX`, `ProviderID`.

> Caveat: the description and unique-row text spell the code variables `Px` and `Px_CodeType`, while the variable table uses `PX` and `PX_CodeType`. Pick one spelling and apply it consistently in your implementation.

**Numeric lengths:** `Num(#)` variables take an as-needed length — use the fewest bytes that hold all distinct values. A numeric variable of a given byte length stores integers exactly up to: 3 bytes → 8,192; 4 → 2,097,152; 5 → 536,870,912; 6 → 137,438,953,472; 7 → 35,184,372,088,832; 8 → 9,007,199,254,740,992.

## Variables

### `PatID`
**Num(#)** (as-needed length) · Missing: never. Example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables.

`PatID` is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.

### `EncounterID`
**Num(#)** (as-needed length) · Missing: never. Example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, Prescribing, and Patient-Reported Measures Response tables.

For efficiency, medical utilization data is captured in three tables: **Encounter** (the record characterizing the outpatient visit or hospital stay), **Diagnosis** (the diagnosis codes associated with the encounter record), and **Procedure** (the procedure codes associated with the encounter record). All diagnoses and procedures for an encounter should have the same `EncounterID`. "Orphan" diagnosis or procedure records whose `EncounterID` has no match in the Encounter table are allowable. The Encounter, Diagnosis, Procedure, Inpatient Transfusion, Inpatient Pharmacy, Vital Signs, Mother-Infant Linkage, and Prescribing tables are linked by `PatID` and `EncounterID`.

### `ADate`
**Num(4)** · Missing: never. Example: `12/24/2019`

Encounter or admission date. Values are SAS dates. Must match `ADate` in the Encounter table for the same `PatID`/`EncounterID` combination.

### `ProviderID`
**Num(#)** (as-needed length) · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `99218766`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Unique provider identifier for the individual provider who made the diagnosis. Like `PatID`, it is a pseudo-identifier with a consistent crosswalk to the real identifier, and it links to the Provider table.

If all `ProviderID` values are unknown (set to `.U`), use `Num(3)`. If the `ProviderID` length is greater than 3 in any other table, set the length to the maximum length found across all tables. Variable lengths must be consistent across tables for same-named variables.

### `EncType`
**Char(2)** · Missing: never. Example: `IP`

Encounter Type. Values:

- `AV` — Ambulatory Visit (includes visits at outpatient clinics, same day surgeries, urgent care visits, and other same-day ambulatory hospital encounters, but excludes Emergency Department encounters. Transfer from an AV facility to an ED facility starts a new encounter at the new facility.)
- `ED` — Emergency Department (includes ED encounters that become inpatient stays through hospital admission: the ED is one encounter, the Inpatient Hospital Stay after admission from the ED is a second encounter. ED data should be identified before hospitalization data to ensure that ED with subsequent admission won't be rolled up into the hospital event. Transfer from one ED facility to another ED facility starts a new encounter at the new facility. Excludes urgent care visits.)
- `IP` — Inpatient Hospital Stay (includes all inpatient stays, same-day hospital discharges, and other acute hospital care where discharge is after the admission date. Transfer from one facility to another starts a new encounter at the new facility.)
- `IS` — Non-Acute Institutional Stay (includes hospice, skilled nursing facility (SNF), rehab center, nursing home, residential, overnight non-hospital dialysis, and other non-hospital stays. Transfer from one facility to another starts a new encounter at the new facility.)
- `OA` — Other Ambulatory Visit (includes other non-overnight `AV` encounters such as hospice visits, home health visits, skilled nursing facility visits, other non-hospital visits, as well as telemedicine, telephone, and email consultations.)

### `PX`
**Char(#)** (as-needed length) · Missing: never. Example: `76815`

Procedure code. Remove decimal points, site-specific suffixes, and prefixes. Other codes should be listed as recorded in the source data; convert local codes to standard codes. Use the fewest number of bytes needed to contain values for all values of `PX_CodeType`.

### `PX_CodeType`
**Char(2)** · Missing: never. Example: `C4`

Procedure code type. Values:

- `09` — ICD-9-CM
- `10` — ICD-10-CM
- `11` — ICD-11-CM
- `C2` — CPT Category II
- `C3` — CPT Category III
- `C4` — CPT-4 (i.e., HCPCS Level I)
- `CP` — Canadian Classification of Diagnostic, Therapeutic, and Surgical Procedures (CCP)
- `CV` — CDC vaccine CVX codes
- `CX` — Canadian Classification of Health Interventions (CCI)
- `H3` — HCPCS Level III
- `HC` — HCPCS (i.e., HCPCS Level II)
- `LC` — LOINC
- `LO` — Local homegrown
- `ND` — NDC
- `OT` — Other
- `RE` — Revenue
- `SK` — SNOMED CT (UK/CPRD)

### `OrigPX`
**Char(#)** (as-needed length) · Missing: yes.

Original procedure code from the source table, if different. Used when a Data Partner must map internal codes to standard codes.
