# Feature Engineering

SCDM 8.2.2

**Purpose:** One record per derived feature.

**Grain:** One record per derived feature.

**Sort order:** PatID.

**Unique row:** FeatureID.

## Variables

Numeric variables declared `Num(#)` use as-needed length: store each value in the fewest bytes that hold all distinct values. Integer capacity by byte length: 3 bytes hold integers up to 8,192; 4 bytes up to 2,097,152; 5 bytes up to 536,870,912; 6 bytes up to 137,438,953,472; 7 bytes up to 35,184,372,088,832; 8 bytes up to 9,007,199,254,740,992.

### `PatID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables. See note 1.

### `EncounterID`

`Num(#)` · length: fewest bytes that hold all distinct values · missing allowed · example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, Prescribing, and Patient-Reported Outcomes Response tables. See note 2.

> Source-document name variant: this definition names the "Patient-Reported Outcomes Response" table, while other source references call it the Patient-Reported Measures Response table. Both refer to the same companion table; the differing names are retained as published.

### `FeatureID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `54321`

Unique feature engineering results/row identifier. Used to link the record to a feature engineering metadata table locally maintained by the Data Partner, storing information such as "NLP Pipeline Name", "NLP Version", "Date NLP Run", NLP model validation metadata, and/or settings.

### `Feature_dt`

`Num(4)` · never missing · example: `12/24/2019`

Date of the recorded feature, stored as a SAS date. Where no date is assigned to the feature itself, populate with the date the note was entered.

### `Feature`

`Char(#)` · length: as needed · never missing · example: `P015`

Feature engineering code or standard term. Remove decimal points, site-specific suffixes, and prefixes. Other codes should be listed as recorded in the source data.

### `FE_CodeType`

`Char(2)` · never missing · example: `10`

Feature engineering code type. Used together with the Feature field to capture any type of diagnosis or clinical concept available in the source data. Other code types may be added as new terminologies are used.

Values:

- `SM` — SNOMED CT
- `10` — ICD-10-CM
- `11` — ICD-11-CM9
- `MD` — MedDRA
- `UC` — UMLS CUI

> Source-document anomaly: code `11` is labelled "ICD-11-CM9", which matches no standard terminology name. The label is retained verbatim rather than corrected.

### `ProviderID`

`Num(#)` · SAS7BDAT: ordinary missing not allowed; `.U` allowed · example: `99218766`

Unique provider identifier. Identifier for the individual provider who made the diagnosis. As with PatID, ProviderID is a pseudo-identifier with a consistent crosswalk to the real identifier. If an individual provider cannot be identified, populate ProviderID with the special missing value `.U`. Links to the Provider table.

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

### `Confidence`

`Char(1)` · never missing · example: `E`

Confidence that the feature derived from source data is accurate. If uncertain, use `N` = Not Assessed.

Values:

- `E` — Excellent
- `F` — Fair
- `P` — Poor
- `N` — Not Assessed

### `Feature_Status`

`Char(1)` · never missing · example: `A`

Indicators used to modify the derived feature based on context in the source note. Where the feature is present for the individual patient on the feature date and no negating criteria exist, status should be `A`. Where the feature is historically present for the individual patient, status should be `H`. Where the feature is ruled out, status should be `N`. Where the feature is not associated with the patient, status should be `X`.

Values:

- `A` — Active
- `H` — Historical
- `N` — Negated
- `X` — Non-patient (e.g. Family History)
- `U` — Unknown

## Notes

1. PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.
2. EncounterID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For features not linked to an explicit Sentinel encounter, the EncounterID may be left missing.
