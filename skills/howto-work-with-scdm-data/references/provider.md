# Provider

Version 8.2.2 · TabID PVD

One record per individual provider identifier referenced by other tables.

Should capture all uniquely recorded individual provider identifiers in the Dispensing, Diagnosis, Procedure, and Prescribing (if populated) tables.

**Sort order:** ProviderID

**Unique row:** ProviderID

## Variables

### `ProviderID`

Num(#) · SAS7BDAT: ordinary missing not allowed; `.U` allowed

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Unique provider identifier for the individual provider who wrote a prescription in the Prescribing table, was identified in a row in the Dispensing table, made a diagnosis in the Diagnosis table, or performed a procedure or service in the Procedure table. Like PatID, the provider identifier is a pseudo-identifier with a consistent crosswalk to the real identifier. Choose the fewest bytes that hold all distinct values exactly: Num(3) holds up to 8,192; Num(4) up to 2,097,152; Num(5) up to 536,870,912; Num(6) up to 137,438,953,472; Num(7) up to 35,184,372,088,832; Num(8) up to 9,007,199,254,740,992.

ProviderID links to the same-named variable in the Dispensing, Diagnosis, Procedure, and — if populated — Prescribing tables, and must be the same length across all tables.

If ALL ProviderID values in the other tables are `.U`, still create a Provider table that meets these requirements:

- Only 1 row in the table
- ProviderID has a length of Num(3) (holds up to 8,192)
- ProviderID is set to `.U`
- Specialty has a length of Char(2)
- Specialty is set to `99`
- Specialty_CodeType is set to `2`

Example: `99218766`

### `Specialty`

Char(#) · missing not allowed

Code indicating the medical specialty of the provider, drawn from the PVD.specialty reference table. If an individual provider has more than one specialty, select the specialty under which the provider practices most often. Map to the most specific description possible: a 10-character Taxonomy Code if possible; otherwise use a 2-character specialty code. The code width is 2 or 10 characters per row, matching Specialty_CodeType; SAS character columns are fixed-width, so size the column for the widest code stored (Char(2) if only 2-character codes, Char(10) if any 10-character codes are present). Use `99` for unknown or undefined individual provider specialty.

Allowed values: see [allowed specialties](provider-specialties.md). That list is a subset of the CMS Provider Taxonomy and Specialty Codes crosswalk; use only values found in it.

Example: `45` or `207X00000X`

### `Specialty_CodeType`

Char(1) · missing not allowed

Indicates which code style Specialty holds.

Values: `2` = 2-character code (from the allowed specialties list) · `0` = 10-character code (from the allowed specialties list)

Example: `2`
