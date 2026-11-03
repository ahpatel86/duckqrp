# Inpatient Transfusion

SCDM 8.2.2 · TabID TXN

**Purpose:** Inpatient blood transfusion administration records.

**Grain:** One record per unique inpatient transfusion administration.

**Sort order:** PatID, then TDate_Start.

**Unique row:** TransID.

> Source-document conflict, unresolved: the description states uniqueness two ways — one record per unique combination of PatID and TransID, and unique value combinations of PatID/TransCode/TransCode_Type/TDate_Start/TTime_Start — while the Unique Row field names TransID alone. No single reading is authoritative in the documentation.

## Variables

Numeric variables declared `Num(#)` use as-needed length: store each value in the fewest bytes that hold all distinct values. Integer capacity by byte length: 3 bytes hold integers up to 8,192; 4 bytes up to 2,097,152; 5 bytes up to 536,870,912; 6 bytes up to 137,438,953,472; 7 bytes up to 35,184,372,088,832; 8 bytes up to 9,007,199,254,740,992.

### `PatID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables. See note 1.

### `EncounterID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, and Prescribing tables. See note 2.

### `TransID`

`Char(15)` · length: fewest bytes that hold all distinct values · never missing · example: `123456789012345`

Unique transfusion administration row identifier. Retain because useful to map back to source data.

### `TransCode`

`Char(15)` · never missing · example: `123451224200599`

Code value for an infusion product. Must be paired with the correct TransCode_Type.

### `TransCode_Type`

`Char(2)` · never missing · example: `CD`

Transfusion product code type. Used together with TransCode to capture any type of inpatient infusion product in the source data. Other code types will be added as new terminologies are used.

Values:

- `CD` — CODABAR
- `IS` — ISBT

### `Orig_TransProd`

`Char(#)` · length: site-specific, as needed · missing allowed · example: `Thawed POOLED PLATELETS`

Original product name/mnemonic within the Data Partner. Site-specific length.

### `BloodType`

`Char(3)` · never missing · example: `AB+`

Blood type and Rh factors, without leading spaces. Convert any text Rh factor to symbols (for example, "pos" to `+`, "negative" to `-`). Rh factor can be blank.

Values: A, B, O, or AB (upper case) with Rh factor `+`, `-`, or blank only; or `UN` = Unknown.

### `TDate_Start`

`Num(4)` · never missing · example: `12/1/2015`

Administration start date, stored as a SAS date value.

### `TTime_Start`

`Num(4)` · missing allowed · example: `14:27`

Administration start time, stored as a SAS time value (HH:MM).

### `TDate_End`

`Num(4)` · missing allowed · example: `12/1/2015`

Administration end date, stored as a SAS date value.

### `TTime_End`

`Num(4)` · missing allowed · example: `20:56`

Administration end time, stored as a SAS time value (HH:MM).

### `EncType`

`Char(2)` · missing allowed · example: `IP`

Encounter type.

Values:

- `ED` — Emergency Department. Includes ED encounters that become inpatient stays through hospital admission. In this scenario, ED is one encounter and the Inpatient Hospital Stay after admission from the ED is a second encounter. ED data should be identified before hospitalization data to ensure that ED with subsequent admission won't be rolled up in the hospital event. Transfer from one ED facility to another ED facility starts a new encounter at the new facility. Excludes urgent care visits.
- `IP` — Inpatient Hospital Stay. Includes all inpatient stays, same-day hospital discharges, and other acute hospital care where the discharge is after the admission date. Transfer from one facility to another facility starts a new encounter at the new facility.
- `IS` — Non-Acute Institutional Stay. Includes hospice, skilled nursing facility (SNF), rehab center, nursing home, residential, overnight non-hospital dialysis, and other non-hospital stays. Transfer from one facility to another facility starts a new encounter at the new facility.
- `OA` — Other Ambulatory Visit. Includes other non-overnight AV encounters such as hospice visits, home health visits, skilled nursing facility visits, and other non-hospital visits, as well as telemedicine, telephone, and email consultations.

## Notes

1. PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.
2. The Encounter, Diagnosis, Procedure, Inpatient Transfusion, Inpatient Pharmacy, Vital Signs, Mother-Infant Linkage, and Prescribing tables are linked by PatID and EncounterID.
