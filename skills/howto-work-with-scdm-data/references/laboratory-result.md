# Laboratory Result

SCDM 8.2.2 · TabID LAB

One record per resulted laboratory test.

The table contains one record per result/entry. Include only resulted lab tests. Only records with actual lab results belong here: if the result suggests the test was run (e.g., the result is "borderline" or "inconclusive"), include it. If the test was not resulted for any reason (specimen not sufficient, patient did not show), do not include it. Negative numeric values (e.g., -5.0 mg/ml) are not included.

**Sort:** `PatID`.

**Unique row:** `LabID`.

**Numeric lengths:** `Num(#)` variables take an as-needed length — use the fewest bytes that hold all distinct values. A numeric variable of a given byte length stores integers exactly up to: 3 bytes → 8,192; 4 → 2,097,152; 5 → 536,870,912; 6 → 137,438,953,472; 7 → 35,184,372,088,832; 8 → 9,007,199,254,740,992.

## Variables

### `PatID`
**Num(#)** (as-needed length) · Missing: never. Example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables.

`PatID` is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.

### `LabID`
**Num(#)** (as-needed length) · Missing: never. Example: `027`

Unique laboratory result/row identifier. Useful to map back to source data.

### `MS_Test_Name`
**Char(20)** · Missing: never. Example: `ALP`

Abbreviated test name. Populated for all records. Several LOINC codes (`LOINC`) and/or local codes (`LOCAL_CD`) can point to one `MS_Test_Name`.

Values:

- `ALP` — alkaline phosphatase
- `ALT` — alanine aminotransferase
- `ANC` — absolute neutrophil count
- `BILI_TOT` — total bilirubin
- `CHOL_HDL` — cholesterol high density lipoprotein
- `CHOL_LDL` — cholesterol low density lipoprotein
- `CHOL_TOT` — cholesterol total
- `CK` — creatine kinase total
- `CK_MB` — creatine kinase MB
- `CK_MBI` — creatine kinase MB/creatine kinase total
- `CREATININE` — creatinine
- `D_DIMER` — d-dimer
- `GLUCOSE` — glucose
- `HGB` — hemoglobin
- `HGBA1C` — glycosylated hemoglobin
- `INF_A` — influenza virus A
- `INF_AB` — influenza virus A + B
- `INF_B` — influenza virus B
- `INF_NS` — influenza virus not specified
- `INR` — international normalized ratio
- `LIPASE` — lipase
- `PG` — pregnancy test
- `PLATELETS` — platelet count
- `SARS_COV_2` — sars coronavirus-2
- `SARS_COV_2_AB_TOTAL` — sars coronavirus-2 antibody
- `SARS_COV_2_AB_A` — sars coronavirus-2 antibody IgA
- `SARS_COV_2_AB_G` — sars coronavirus-2 antibody IgG
- `SARS_COV_2_AB_M` — sars coronavirus-2 antibody IgM
- `SARS_COV_2_AB_AM` — sars coronavirus-2 antibody IgA + IgM
- `SARS_COV_2_AB_GM` — sars coronavirus-2 antibody IgG + IgM
- `SARS_COV_2_AB_NS` — sars coronavirus-2 antibody not specified
- `SODIUM` — sodium
- `TRIG` — triglycerides
- `TROP_I` — troponin I cardiac
- `TROP_T` — troponin T cardiac
- `TSH` — thyroid stimulating hormone
- `UNMAPPED` — LOINC code not mapped to another `MS_Test_Name` should be mapped to this value

### `Result_Type`
**Char(1)** · Missing: never. Example: `N`

Indicates whether the laboratory test result in the Data Partner's source data is Numeric (e.g., 100 ug/mL, <100 ug/mL, or >100 ug/mL) or Character (e.g., +, POS, POSITIVE, or ranges such as 50-100 mg/mL). Unknown value is populated when `MS_Test_Name` = `UNMAPPED`.

Values:

- `N` — numeric
- `C` — character
- `U` — unknown

### `MS_Test_Sub_Category`
**Char(6)** · Missing: yes, conditioned on `MS_Test_Name`. Example: `PCR`

Sub-category for `MS_Test_Name`. Sub-categories apply to only select laboratory tests. `DIRECT` and `CLC` are populated only for `MS_Test_Name` = `CHOL_LDL`. `DDU` and `FEU` are populated only for `MS_Test_Name` = `D_DIMER` with `Result_Type` = `N`. `BHCG` and `HCG` are populated only for `MS_Test_Name` = `PG`.

Values:

- `BHCG` — beta human choriogonadotropin
- `CLC` — calculated
- `DDU` — d-dimer units
- `DIRECT` — direct
- `EIA` — enzyme immunoassay
- `FEU` — fibrinogen equivalent units
- `HCG` — human choriogonadotropin
- `IA_RAP` — immunoassay, rapid
- `IF` — immunofluorescence
- `NS` — not specified
- `PCR` — probe and target amplification
- `SEQ` — sequencing
- `VTC` — organism-specific culture

### `Fast_Ind`
**Char(1)** · Missing: never. Example: `F`

Fasting Indicator. Not all laboratory tests are differentiated by fasting status. For laboratory tests where fasting status is not considered when interpreting results, `Fast_Ind` is marked `X` (not applicable). For laboratory test results where fasting status may be considered when interpreting results (e.g., glucose), `Fast_Ind` is marked `F` (fasting) only if the source data included with the laboratory test result indicates a fasting test. All other laboratory test results where fasting status may be considered but are not explicitly marked as fasting are marked `R` (random). Unknown value is populated when `MS_Test_Name` = `UNMAPPED`.

Values:

- `F` — fasting
- `R` — random
- `X` — not applicable
- `U` — unknown

### `Specimen_Source`
**Char(7)** · Missing: never. Example: `SERUM`

Specimen source. Populated for all records. Some laboratory tests have several possible valid values for `Specimen_Source`.

Values:

- `BAL` — bronchoalveolar lavage
- `BALBX` — bronchoalveolar biopsy
- `BLD_DOT` — blood dot
- `BLOOD` — blood
- `CSF` — cerebrospinal fluid
- `NPH` — nasopharyngeal swab
- `NPWASH` — nasopharyngeal wash
- `NSWAB` — nasal swab or nose specimen
- `NWASH` — nasal wash
- `OTHER` — other
- `PLASMA` — plasma
- `PPP` — platelet poor plasma
- `SALIVA` — saliva sample
- `SERUM` — serum
- `SPUTUM` — sputum
- `SR_PLS` — serum/plasma
- `THRT` — throat swab, oropharyngeal swab
- `UNK` — unknown or missing
- `URINE` — urine

### `LOINC`
**Char(10)** · Missing: yes. Example: `16182-8`

Logical Observation Identifiers, Names, and Codes (LOINC) code. LOINC is a universal coding system for tests, measurements, and observations developed by the Regenstrief Institute; it provides granular information such as the long name of the test, test definition/description, component, system, property, timing, scale, method, and examples of units. Include all parts of the LOINC code, including the hyphen — the last digit is a check digit and is always preceded by a hyphen. If a record utilizes a local LOINC (e.g., LOINC candidate codes), the result of that record is included but the `LOINC` variable is set to null. Values do not contain leading zeros. LOINC is populated when available, so not all records have it.

### `Stat`
**Char(1)** · Missing: never. Example: `E`

Immediacy of test. The intent is to determine whether the test was obtained as part of routine care or as an emergent/urgent diagnostic test (designated as Stat or Expedite). Unknown value is populated when `MS_Test_Name` = `UNMAPPED`.

Values:

- `E` — Expedite
- `R` — Routine
- `S` — Stat
- `U` — Unknown or null

### `Pt_Loc`
**Char(1)** · Missing: never. Example: `O`

Patient location where the lab specimen was obtained. Unknown value is populated when `MS_Test_Name` = `UNMAPPED`.

Values:

- `E` — Emergency department
- `H` — Home
- `I` — Inpatient
- `O` — Outpatient
- `U` — Unknown or null

### `Result_Loc`
**Char(1)** · Missing: no. Example: `L`

Location of the test result. Point of Care locations may include anticoagulation clinic, newborn nursery, finger stick in provider office, or home. The default value is `L` unless the result is Point of Care.

> Caveat: the source documentation marks missing values as allowed for this variable while its definition also states "There are not any null values." Both statements are preserved here as written.

Values:

- `L` — Lab
- `P` — Point of Care

### `LOCAL_CD`
**Char(#)** (as-needed length) · Missing: yes.

Local code (non-LOINC) related to an individual lab test; values are unique to each Data Partner. Not required; populated only if available in source data. This variable is not used in queries, but may be used by local programmers to identify and extract the required CDM tests.

### `BATTERY_CD`
**Char(#)** (as-needed length) · Missing: yes.

Local code (non-LOINC) related to a battery or panel of lab tests; values are unique to each Data Partner. Not required; populated only if available in source data. This variable is not used in queries, but may be used by local programmers to identify and extract the required CDM tests.

### `PX`
**Char(#)** (as-needed length) · Missing: conditionally required if `PX_CodeType` is populated. Example: `76815`

Procedure code associated with the laboratory result record. Not required; populated only if available in source data. `PX` is populated if `PX_CodeType` is populated.

### `PX_CodeType`
**Char(2)** · Missing: conditionally required if `PX` is populated. Example: `C4`

Procedure code type associated with the laboratory result record. Not required; populated only if available in source data. `PX_CodeType` is populated if `PX` is populated.

Values:

- `09` — ICD-9-CM
- `10` — ICD-10-CM
- `11` — ICD-11-CM
- `C2` — CPT Category II
- `C3` — CPT Category III
- `C4` — CPT-4 (i.e., HCPCS Level I)
- `CP` — Canadian Classification of Diagnostic, Therapeutic, and Surgical Procedures (CCP)
- `CX` — Canadian Classification of Health Interventions (CCI)
- `H3` — HCPCS Level III
- `HC` — HCPCS (i.e., HCPCS Level II)
- `LO` — Local homegrown
- `RE` — Revenue
- `SK` — SNOMED CT (UK/CPRD)
- `OT` — Other

### `Order_dt`
**Num(4)** · Missing: yes. Example: `11/9/2009`

Date that the test was ordered, as a SAS date. This date could fall anywhere from the same day the specimen was collected to months before the specimen was collected.

### `Lab_dt`
**Num(4)** · Missing: yes. Example: `11/21/2009`

Date that the specimen was collected, as a SAS date. For most Sentinel activities, this is the most relevant date.

### `Lab_tm`
**Num(4)** · Missing: yes. Example: `15:15`

Time of day that the specimen was collected, as a SAS time value (HH:MM). Valid values are between 00:00 and 23:59. `Lab_tm` is associated with `Lab_dt`.

### `Result_dt`
**Num(4)** · Missing: yes. Example: `11/22/2009`

Date that the laboratory test was resulted, as a SAS date. Depending on the time of the test, this date could be the same day the specimen was collected or any date up to weeks later.

Three dates can be associated with laboratory results: order date (`Order_dt`), laboratory date (`Lab_dt`), and result date (`Result_dt`). The typical sequence, earliest to latest, is: `Order_dt` ≤ `Lab_dt` ≤ `Result_dt`. One or more of these dates is populated for each record; all are populated when available in source data.

### `Result_tm`
**Num(4)** · Missing: yes. Example: `2:33`

Time that the laboratory test was resulted, represented as a SAS time value (format HHMM). Valid values are between 00:00 and 23:59. `Result_tm` is associated with `Result_dt`.

### `Orig_Result`
**Char(50)** · Missing: never. Example: `+`

Populated for all records. If `Result_Type` = `N`, the value represents the numeric portion of the test result, stripped of any modifiers (e.g., >, LE, GT) and/or result units (e.g., ng/ml, cells/mm3, %). If `Result_Type` = `C`, the value reflects the test string based on source data; values may include a decimal point (`.`), a sign (`-`, `+`), or text (e.g., POSITIVE, NEGATIVE, DETECTED). For records where `Result_Type` = `N`, the symbols >, <, >=, <= are removed from the value and stored in `Modifier`, and result units are removed and stored in `Orig_Result_unit`. Unknown value is populated when `MS_Test_Name` = `UNMAPPED`.

### `MS_Result_C`
**Char(50)** · Missing: yes, unless `Result_Type` = `C`. Examples: `POSITIVE`; `50|100 mg/mL`

Standardized result value, populated only for text or character results (`Result_Type` = `C`) and null for numeric results (`Result_Type` = `N`). If `Result_Type` = `C` and the source result is a range (e.g., 50-100 mg/mL), populate using `Orig_Result` with the start and end values of the range delimited by a vertical bar (e.g., "50-100 mg/mL" becomes `50|100 mg/mL`).

Values:

- `BORDERLINE`
- `NEGATIVE`
- `POSITIVE`
- `UNDETERMINED`
- Range format: `start|end[unit]`

### `MS_Result_N`
**Num(8)** · Missing: yes, unless `Result_Type` = `N`. Example: `100`

Standardized/converted numeric result for records where `Result_Type` = `N`. Acceptable values are numeric digits with or without a decimal ("."). If the standardized result unit (`Std_Result_unit`) differs from an acceptable `MS_Result_unit` for a numeric test, `Orig_Result` is converted before populating `MS_Result_N`, and `MS_Result_unit` reflects this conversion. Populated only for numeric results and does not contain negative values; null for text/character results (`Result_Type` = `C`).

### `Modifier`
**Char(2)** · Missing: never. Example: `LE`

Modifier for result values. Any relational operators in the original source data value (e.g., <, >, or =) are reflected here. For example, if the original source data value is <=200, then `Orig_Result` = `200` and `Modifier` = `LE`. If the original source data result value is text, then `Modifier` = `TX`. If it is numeric (digits with or without decimal) and contains no operator, then `Modifier` = `EQ`. Unknown value is populated when `MS_Test_Name` = `UNMAPPED`.

Values:

- `EQ` — equal
- `GE` — greater than or equal to
- `GT` — greater than
- `LE` — less than or equal to
- `LT` — less than
- `TX` — text
- `UN` — unknown

### `Orig_Result_unit`
**Char(20)** · Missing: yes. Example: `10^9/L`

Original units for the test result, as reported in source data. Directly related to `Orig_Result` and `Modifier`. Does not include the test name or any special characters unless the character is part of the unit value: special characters are included in `10^9/L`, but not in "^U/L^", where the carets are not part of the unit value. Some laboratory tests may not have a result unit.

### `Std_Result_unit`
**Char(11)** · Missing: yes. Example: `CELL/MM3`

Standardized units for the result. Purpose: modify `Orig_Result_unit` from free text to a standardized unit, as an intermediary step in converting from `Orig_Result_unit` to `MS_Result_unit`. Populated only for records where `Result_Type` = `N`. Common rules: convert all text values of `Orig_Result_unit` to uppercase, and use standard abbreviations as provided by SOC. Does not include special characters unless the character is part of the unit (as with `10^9/L` vs. "^U/L^"). Not usually null, though there are exceptions — for example, when the test result is a ratio (e.g., International Normalized Ratio [INR]). Null for character tests (`Result_Type` = `C`) until that test has been reviewed and characterized by the Clinical Data Elements Workgroup.

### `MS_Result_unit`
**Char(11)** · Missing: yes, unless `Result_Type` = `N`. Example: `G/L`

Converted/standardized result units for the value populated in `MS_Result_N`. Null for records where `Result_Type` = `C`, and null for laboratory tests that have not been characterized and reviewed by the Clinical Data Elements Workgroup. May be null for some numeric tests (e.g., International Normalized Ratio [INR] is a ratio and has no result unit). For tests that require a result unit, set to `UNKNOWN` for all records where the original result unit is missing or blank, "NULL", "N/A", "NA", or "UNK". Guidance is provided only for laboratory tests that have been reviewed by the Clinical Data Elements Workgroup.

### `Norm_Range_low`
**Char(8)** · Missing: yes, unless `Result_Type` = `N`. Example: `100`

Lower bound of the normal reference range, as assigned by the laboratory. The laboratory-assigned normal range is parsed into `Norm_Range_low`, `Modifier_low`, `Norm_Range_high`, and `Modifier_high`, reflecting what is seen in source data. The value contains only the lower bound: it is not converted, includes no unit of measure (the associated unit is assumed to be the same as the original result unit from source data), and the symbols >, <, >=, <= are removed. For example, if the normal range for a test is >100 and <300, enter `100`. Null for records where `Result_Type` = `C`.

### `Modifier_low`
**Char(2)** · Missing: conditionally required when `Result_Type` = `N`. Example: `EQ`

Modifier for `Norm_Range_low` values. Null for records where `Result_Type` = `C`. For numeric results, one of the following must hold:

1. Both `Modifier_low` and `Modifier_high` contain `EQ` (e.g., normal values fall in the range 3-10).
2. `Modifier_low` contains `GT` or `GE` and `Modifier_high` is null (e.g., normal values are >3 with no upper boundary).
3. `Modifier_high` contains `LT` or `LE` and `Modifier_low` is null (e.g., normal values are <=10 with no lower boundary).

Values:

- `EQ` — equal
- `GE` — greater than or equal to
- `GT` — greater than

### `Norm_Range_high`
**Char(8)** · Missing: yes, unless `Result_Type` = `N`. Example: `300`

Upper bound of the normal reference range, as assigned by the laboratory. The laboratory-assigned normal range is parsed into `Norm_Range_low`, `Modifier_low`, `Norm_Range_high`, and `Modifier_high`, reflecting what is seen in source data. The value contains only the upper bound: it is not converted, includes no unit of measure (the associated unit is assumed to be the same as the original result unit from source data), and the symbols >, <, >=, <= are removed. For example, if the normal range for a test is >100 and <300, enter `300`. Null for records where `Result_Type` = `C`.

### `Modifier_high`
**Char(2)** · Missing: yes, unless `Result_Type` = `N`. Example: `LT`

Modifier for `Norm_Range_high` values. Null for records where `Result_Type` = `C`. For numeric results, one of the following must hold:

1. Both `Modifier_low` and `Modifier_high` contain `EQ` (e.g., normal values fall in the range 3-10).
2. `Modifier_low` contains `GT` or `GE` and `Modifier_high` is null (e.g., normal values are >3 with no upper boundary).
3. `Modifier_high` contains `LT` or `LE` and `Modifier_low` is null (e.g., normal values are <=10 with no lower boundary).

Values:

- `EQ` — equal
- `LE` — less than or equal to
- `LT` — less than

### `Abn_ind`
**Char(2)** · Missing: never. Example: `CR`

Abnormal result indicator. The value comes directly from the source data; it is not created programmatically based on variables such as `MS_Result_N`, `Norm_Range_high`, or `Norm_Range_low`. Unknown value is populated when `MS_Test_Name` = `UNMAPPED`.

Values:

- `AB` — abnormal
- `AH` — abnormally high
- `AL` — abnormally low
- `CH` — critically high
- `CL` — critically low
- `CR` — critical
- `IN` — inconclusive
- `NL` — normal
- `UN` — unknown

### `Order_dept`
**Char(#)** (as-needed length) · Missing: yes.

Local code for the ordering provider department; values are unique to each Data Partner. Not required; populate only if the ordering provider department is available in source data.

### `FacilityID`
**Num(#)** (as-needed length) · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `12345678`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Servicing facility identifier. Local facility pseudo-identifier that identifies the hospital or clinic, taken from facility claims. Used for chart abstraction and validation, and links to the Facility table.

If all `FacilityID` values are unknown (set to `.U`), use `Num(3)`. If the `FacilityID` length is greater than 3 in any other table, set the length to the maximum length found across all tables. Variable lengths must be consistent across tables for same-named variables.
