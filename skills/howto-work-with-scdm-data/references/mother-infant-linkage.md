# Mother-Infant Linkage

SCDM 8.2.2

**Purpose:** Links mothers with evidence of live birth delivery to their infants.

**Grain:** One record per MPatID, CPatID, and ADate. The table is created after identifying mothers (via evidence of live birth delivery by individuals between 10 and 54 years of age as of the admission date of the delivery encounter, with an assigned value of `Female` in the SCDM Demographic table variable Sex) and infants (via date of birth) in the Sentinel Distributed Database (SDD). The file may include:

1. Live birth deliveries (with MPatID and ADate) that were linked to an infant (CPatID).
2. Live birth deliveries (with MPatID and ADate) that were not linked to an infant (CPatID, CBirth_Date, Sex, and CEnr_Start will have missing values).
3. Infants (with CPatID) who were not linked to a mother (MPatID, MBirth_Date, Age, EncounterID, EncType, ADate, DDate, Birth_Type, and Birth_Type_Primes will have missing values).

**Sort order:** MPatID, then ADate, then CPatID.

**Unique row:** MPatID, EncounterID, CPatID.

## Variables

Numeric variables declared `Num(#)` use as-needed length: store each value in the fewest bytes that hold all distinct values. Integer capacity by byte length: 3 bytes hold integers up to 8,192; 4 bytes up to 2,097,152; 5 bytes up to 536,870,912; 6 bytes up to 137,438,953,472; 7 bytes up to 35,184,372,088,832; 8 bytes up to 9,007,199,254,740,992.

### `MPatID`

`Num(#)` · length: fewest bytes that hold all distinct values · missing only allowed for infant records · example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables. Must match the mother's PatID value in all other SCDM tables. Blank for infant-only records. See note 1.

### `MBirth_Date`

`Num(4)` · missing only allowed for infant records · example: `12/5/1971`

Mother Birth_Date value from the SCDM Demographic table, stored as a SAS date. Missing for infant-only records.

### `Age`

`Num(3)` · missing only allowed for infant records · example: `32`

Mother's age as of ADate. Values 10–54 inclusive. Missing for infant-only records.

### `EncounterID`

`Num(#)` · length: fewest bytes that hold all distinct values · missing only allowed for infant records · example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, Prescribing, and Patient-Reported Measures Response tables. EncounterID value from the SCDM Encounter table, for the mother's delivery encounter. Missing for infant-only records. See note 2.

### `EncType`

`Char(2)` · missing only allowed for infant records · example: `IP`

EncType value from the SCDM Encounter table, for the mother's delivery encounter. Blank for infant-only records. If more than 1 delivery encounter occurs on the same ADate, EncType values are based on the encounter selected by the hierarchy in note 3.

Values:

- `AV` — Ambulatory Visit
- `ED` — Emergency Department
- `IP` — Inpatient Hospital Stay
- `IS` — Non-Acute Institutional Stay
- `OA` — Other Ambulatory Visit

### `ADate`

`Num(4)` · missing only allowed for infant records · example: `12/24/2005`

ADate value from the SCDM Encounter table, for the mother's delivery encounter, stored as a SAS date. Missing for infant-only records.

### `DDate`

`Num(4)` · missing only allowed for mother records · example: `12/31/2005`

DDate value from the SCDM Encounter table, for the mother's delivery encounter, stored as a SAS date; may be null. Missing for infant-only records.

> Source-document conflict, unresolved: the missing rule says "missing only allowed for Mother records", but the definition says DDate is "missing for infant-only records", and the grain statement lists DDate among the variables that are missing on infant-only records. The two readings cannot both hold; no single reading is authoritative in the documentation.

### `CPatID`

`Num(#)` · length: fewest bytes that hold all distinct values · missing only allowed for mother records · example: `12341234`

Unique member identifier. Arbitrary person-level identifier used to link across tables. Must match the infant's PatID value in all other SCDM tables. Missing for mother/delivery-only records.

### `CBirth_Date`

`Num(4)` · missing only allowed for mother records · example: `1/2/2015`

Infant Birth_Date value from the SCDM Demographic table, stored as a SAS date. Missing for mother/delivery-only records.

### `Sex`

`Char(1)` · missing only allowed for mother records · example: `F`

Infant Sex value from the SCDM Demographic table. Blank for mother/delivery-only records.

Values:

- `A` — Ambiguous
- `F` — Female
- `M` — Male
- `U` — Unknown

### `CEnr_Start`

`Num(4)` · missing only allowed for mother records · example: `1/1/2005`

Earliest Enr_Start from the Enrollment table, stored as a SAS date. Missing for mother/delivery-only records.

### `Birth_Type`

`Num(3)` · missing only allowed for infant records · example: `3`

Number of live births, based on ICD-9-CM/ICD-10-CM codes in the health plan data for the delivery admission. Missing for infant-only records.

Values:

- `0` — Unspecified # of live births
- `1` — 1 live birth
- `2` — 2 live births
- `3` — 3 live births
- `4` — 4 live births
- `5` — 5 live births
- `8` — Multiple live births, unspecified number
- `9` — Conflicting code(s) for number of live births

### `Birth_Type_Primes`

`Num(8)` · missing only allowed for infant records · example: `5`

Multiplication of all prime numbers assigned to all instances of Birth_Type found in delivery codes within the selected encounter. This provides a record of all values of Birth_Type for the selected encounter. Missing for infant-only records.

Prime = Birth_Type:

- 2 = Birth_Type 0
- 3 = Birth_Type 1
- 5 = Birth_Type 2
- 7 = Birth_Type 3
- 11 = Birth_Type 4
- 13 = Birth_Type 5
- 17 = Birth_Type 8
- 19 = Birth_Type 9

### `MatchMethod`

`Char(2)` · never missing · example: `RE`

Prioritized method of linkage for the mother-infant match, or reason for an unlinked record. For linked records, prioritize so that only one method is listed: RE > SI > LA > BC > OT. For cases where a mother/delivery is not linked to an infant, or an infant is not linked to a mother/delivery, the value must be one of N1, N2, N3, or NA only.

Values:

- `BC` — Birth Certificate
- `RE` — DP maintained birth registry
- `SI` — health plan subscriber or family number
- `LA` — exact or probabilistic last name and address match based upon health plan administrative data
- `OT` — other
- `N1` — No subscriber/family IDs available for linkage
- `N2` — No name/address available for linkage
- `N3` — Neither subscriber/family IDs nor name/address available for linkage
- `NA` — no linkage made; any other reason

## Notes

1. PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.
2. The Encounter, Diagnosis, Procedure, Inpatient Transfusion, Inpatient Pharmacy, Vital Signs, Mother-Infant Linkage, and Prescribing tables are linked by PatID and EncounterID.
3. If more than 1 delivery encounter occurs on the same ADate, then the EncType values are based on the encounter selected from the following hierarchy: IP > ED > AV > IS > OA.
