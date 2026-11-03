# Sentinel Common Data Model reference

SCDM 8.2.2. These Markdown files contain the table and variable definitions; read them directly. Each table file includes its row meaning, sort order, uniqueness rules, variable types and byte lengths, missing-value rules, value codes, and guidelines. No scripts, lookup snapshots, or repository files are needed.

## Find a table

| Table | TabID | Reference |
|---|---|---|
| Enrollment | ENR | [enrollment.md](enrollment.md) |
| Demographic | DEM | [demographic.md](demographic.md) |
| Dispensing | DIS | [dispensing.md](dispensing.md) |
| Encounter | ENC | [encounter.md](encounter.md) |
| Diagnosis | DIA | [diagnosis.md](diagnosis.md) |
| Procedure | PRO | [procedure.md](procedure.md) |
| Prescribing | PRE | [prescribing.md](prescribing.md) |
| Facility | FAC | [facility.md](facility.md) |
| Provider | PVD | [provider.md](provider.md) |
| Laboratory Result | LAB | [laboratory-result.md](laboratory-result.md) |
| Vital Signs | VIT | [vital-signs.md](vital-signs.md) |
| Death | DTH | [death.md](death.md) |
| Cause of Death | COD | [cause-of-death.md](cause-of-death.md) |
| Inpatient Pharmacy | IRX | [inpatient-pharmacy.md](inpatient-pharmacy.md) |
| Inpatient Transfusion | TXN | [inpatient-transfusion.md](inpatient-transfusion.md) |
| Mother-Infant Linkage | Not specified | [mother-infant-linkage.md](mother-infant-linkage.md) |
| Patient-Reported Measures Survey | PRS | [prm-survey.md](prm-survey.md) |
| Patient-Reported Measures Survey Response | PRR | [prm-response.md](prm-response.md) |
| Feature Engineering | Not specified | [feature-engineering.md](feature-engineering.md) |

Read the relevant table file. If only a variable name is known, search the Markdown files for its heading. Variable definitions are local to their table: the same name can have different constraints elsewhere.

Long value lists are separate text references: [prescribing routes, forms, and units](prescribing-values.md), [provider specialties](provider-specialties.md), and [PRM questionnaire codes](prm-codes.md). Open these only when the allowed codes or their meanings are needed.

## Read the definitions

- `Char(n)` and `Num(n)` give SAS storage length in bytes, not display width. `Num(#)` means choose the fewest bytes that preserve the required values exactly.
- SAS dates count days from 1 January 1960; SAS times count seconds from midnight. Display examples are not storage formats.
- Special missing values such as `.U` and `.S` apply only to SAS7BDAT files. In Parquet, they become ordinary nulls; their distinct meanings are no longer retained. A variable that permits special missing can therefore contain null in Parquet even when ordinary SAS missing is disallowed.
- Sort order is not the uniqueness key. A shared identifier is not by itself a guaranteed one-to-one join.
- Named clinical vocabularies (such as ICD, LOINC, and RxNorm) identify coding systems, not exhaustive code lists supplied by this reference.
- A stated documentation ambiguity is unresolved; do not turn it into an invented validation rule.
