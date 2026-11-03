# Prescribing

SCDM 8.2.2 · TabID PRE

One record per prescription order.

The table contains one record per `PatID` and `PrescribingID`. It should capture all uniquely recorded prescription orders, as a unique combination of `PatID`, `OrderDate`, `Rx_CodeType`, `Rx`, and `RxRefills`.

**Sort:** `PatID`, then `OrderDate`.

**Unique row:** `PrescribingID`.

> Caveat: the description says records are unique by `PatID`, `OrderDate`, `Rx_CodeType`, `Rx`, and `RxRefills` (one record per `PatID` and `PrescribingID`), while the unique-row rule names `PrescribingID` alone. This tension is present in the source documentation.

**Numeric lengths:** `Num(#)` variables take an as-needed length — use the fewest bytes that hold all distinct values. A numeric variable of a given byte length stores integers exactly up to: 3 bytes → 8,192; 4 → 2,097,152; 5 → 536,870,912; 6 → 137,438,953,472; 7 → 35,184,372,088,832; 8 → 9,007,199,254,740,992.

## Variables

### `PatID`
**Num(#)** (as-needed length) · Missing: never. Example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables.

`PatID` is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.

### `EncounterID`
**Num(#)** (as-needed length) · Missing: yes. Example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, and Prescribing tables.

The Encounter, Diagnosis, Procedure, Inpatient Transfusion, Inpatient Pharmacy, Vital Signs, Mother-Infant Linkage, and Prescribing tables are linked by `PatID` and `EncounterID`.

### `PrescribingID`
**Num(#)** (as-needed length) · Missing: never. Example: `123456789012345`

Unique row identifier. Arbitrary identifier for each unique Prescribing record. It does not need to persist across refreshes and may be created by any method that achieves a unique identifier per row.

### `ProviderID`
**Num(#)** (as-needed length) · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `99218766`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Unique provider identifier, or the special missing `.U` for unknown. Provider code for the individual provider who prescribed the medication. The provider code is a pseudo-identifier with a consistent crosswalk to the real identifier, and it is the same set of pseudo-identifiers used for `ProviderID` in other SCDM tables.

If all `ProviderID` values are unknown (set to `.U`), use `Num(3)`. If the `ProviderID` length is greater than 3 in any other table, set the length to the maximum length found across all tables. Variable lengths must be consistent across tables for same-named variables.

### `OrderDate`
**Num(4)** · Missing: never. Example: `2/16/2011`

Order date of the prescription by the provider. Values are valid SAS dates.

### `Rx_CodeType`
**Char(2)** · Missing: never. Example: `ND`

Code type of prescribed medication or medical device. Combined with `Rx`, this field captures any type of prescribed medication or medical device available in the source data. Other code types may be added as new terminologies are used.

Values:

- `ND` — National Drug Code (US)
- `SN` — SNOMED CT (US)
- `SK` — SNOMED CT (UK/CPRD)
- `DM` — Dictionary of Medicines and Devices (UK)
- `DI` — Drug Identification Number (CA)
- `RN` — RxNorm (US)
- `AT` — Anatomical Therapeutic Chemical Classification (DK)
- `GP` — Generic Product Identifier

### `Rx`
**Char(#)** (as-needed length) · Missing: never. Example: `00006007431`

Drug code representing a prescribed medication or medical device. Expunge any placeholders (e.g., `-` or extra digit). Use the fewest number of bytes needed to contain values for all values of `Rx_CodeType`.

### `RxAmt`
**Num(4)** · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `90`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Quantity ordered. Positive integer values.

### `RxSup`
**Num(4)** · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `90`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Number of days supply ordered, as specified by the prescription. Positive integer values.

### `RxSource`
**Char(2)** · Missing: never. Example: `OD`

Source of the prescribing information. Values:

- `OD` — Order/EHR
- `DR` — Derived
- `UN` — Unknown
- `OT` — Other

### `RxRoute`
**Char(25)** · Missing: never. Example: `ORAL`

Route of medication delivery. Fixed list; see [prescribing-values.md — RxRoute](prescribing-values.md#rxroute).

### `RxDoseQuantity`
**Num(8)** · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `120`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Dose of a given medication, as ordered by the provider. Values greater than zero.

### `RxDoseUnit`
**Char(25)** · Missing: never. Example: `ueq`

Units of measure associated with the dose of the medication (i.e., `RxDoseQuantity`) as ordered by the provider. Fixed list; see [prescribing-values.md — RxDoseUnit](prescribing-values.md#rxdoseunit).

### `RxDoseForm`
**Char(40)** · Missing: yes. Example: `PELLET`

The unit associated with the quantity prescribed; equivalent to RxNorm Dose Form. Only valuable if using RxNorm (`Rx_CodeType` = `RN`). Can be blank. Fixed list; see [prescribing-values.md — RxDoseForm](prescribing-values.md#rxdoseform).

### `RxFreqQuantity`
**Num(4)** · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `2`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Specified frequency of medication per unit of time. Number per unit in `RxFreqUnit`; integer values.

### `RxFreqUnit`
**Char(1)** · Missing: never. Example: `D`

Frequency unit of administration. Values:

- `D` — Per day
- `W` — Per week
- `M` — Per month
- `O` — Other

### `RxRefills`
**Num(3)** · SAS7BDAT: ordinary missing not allowed; `.U` allowed. Example: `2`

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Number of refills ordered, not including the original prescription. If no refills are ordered, the value should be zero.

### `RxPrnFlag`
**Char(1)** · Missing: never. Example: `N`

Flag to indicate that all or part of the medication frequency instructions includes "as needed." Values:

- `Y` — Yes
- `N` — No
- `U` — Unknown

### `RxDAW`
**Char(1)** · Missing: never. Example: `Y`

Flag to indicate whether the provider indicated that the medication order was to be dispensed as written (DAW). Values:

- `Y` — Yes
- `N` — No
- `U` — Unknown
- `O` — Other
