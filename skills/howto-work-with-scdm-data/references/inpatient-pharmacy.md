# Inpatient Pharmacy

SCDM 8.2.2 · TabID IRX

**Purpose:** Inpatient drug administration records.

**Grain:** One record per unique combination of PatID, NDC, RxADate, RxATime, and RxID. Each record represents a unique inpatient pharmacy dispensing administration.

**Sort order:** PatID, then RxADate.

**Unique row:** RxID.

## Variables

Numeric variables declared `Num(#)` use as-needed length: store each value in the fewest bytes that hold all distinct values. Integer capacity by byte length: 3 bytes hold integers up to 8,192; 4 bytes up to 2,097,152; 5 bytes up to 536,870,912; 6 bytes up to 137,438,953,472; 7 bytes up to 35,184,372,088,832; 8 bytes up to 9,007,199,254,740,992.

### `PatID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables. See note 1.

### `EncounterID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, and Prescribing tables. See note 2.

### `NDC`

`Char(11)` · never missing · example: `12345678910`

National Drug Code. Expunge any placeholders (for example, `-` or an extra digit).

### `RxID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing

Unique Rx administration row identifier. Useful to map back to source data.

### `RxADate`

`Num(4)` · never missing · example: `12/1/2019`

Rx administration date, stored as a SAS date value.

### `RxATime`

`Num(4)` · missing allowed · example: `20:56`

Rx administration time, stored as a SAS time value (HH:MM).

### `RxRoute`

`Char(10)` · missing allowed · example: `IV`

Actual/administered route, as described in the source system. A standard list of allowable values is under development.

### `RxDose`

`Num(8)` · missing allowed · example: `100`

Actual/administered dose. Numeric digits with or without a decimal. Intended to be analyzed in conjunction with the RxUOM (unit of measure) field and the product strength data associated with the Drug Code (available from drug databases). The format captures the maximum number of whole and decimal digits allowed by software technology for numeric data.

### `RxUOM`

`Char(10)` · missing allowed · example: `ML`

Actual/administered unit of measure from the source system. Intended to be analyzed in conjunction with the RxDose field and the product strength data associated with the Drug Code (available from drug databases). A standard list of allowable values is under development.

## Notes

1. PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.
2. The Encounter, Diagnosis, Procedure, Inpatient Transfusion, Inpatient Pharmacy, Vital Signs, Mother-Infant Linkage, and Prescribing tables are linked by PatID and EncounterID.
