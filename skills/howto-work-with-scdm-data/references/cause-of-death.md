# Cause of Death

Version 8.2.2 · TabID COD

Cause-of-death codes for deceased patients.

Can contain multiple COD records per PatID. When legacy data have conflicting reports, make a local determination as to which to use. There is typically a 1-2 year lag in death registry data.

For efficiency, death data is captured in 2 tables: Death (the record that characterizes the death date and the source of that information) and Cause of Death (the cause(s) of death associated with the death record). The 2 tables are linked by PatID; all Cause of Death records have a matching PatID in the Death Table.

**Sort order:** PatID

**Unique row:** PatID, CodeType, COD, CauseType, Source

## Variables

### `PatID`

Num(#) · missing not allowed

Unique patient identifier; an arbitrary person-level identifier used to link across tables. Choose the fewest bytes that hold all distinct values exactly: Num(3) holds up to 8,192; Num(4) up to 2,097,152; Num(5) up to 536,870,912; Num(6) up to 137,438,953,472; Num(7) up to 35,184,372,088,832; Num(8) up to 9,007,199,254,740,992.

PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source data partner. Analytical data sets use only the pseudo-identifier to link all information belonging to a patient.

Example: `123456789`

### `COD`

Char(8) · missing not allowed

Cause-of-death diagnosis code. Remove decimal points, site-specific suffixes and prefixes.

Example: `J180`

### `CodeType`

Char(2) · missing not allowed

Cause of death code type.

Values: `09` = ICD-9 · `10` = ICD-10 · `NV` = National Vital Statistics Registry Death Code

Example: `09`

### `CauseType`

Char(1) · missing not allowed

Cause of death type. There should be only one `U` (Underlying) code per PatID.

Values: `C` = Contributory · `I` = Immediate / Primary · `O` = Other · `U` = Underlying

Example: `C`

### `Source`

Char(1) · missing not allowed

Source of cause of death information.

Values: `L` = Other, locally defined · `N` = National Death Registry · `S` = Death files from State/Region/Province · `T` = Tumor data · `O` = Obituary · `M` = Social Media

Example: `S`

### `Confidence`

Char(1) · missing not allowed

Confidence that the patient drawn from the Source data represents the actual patient (contrasts with Confidence in the Death table). If uncertain, use `P`.

Values: `E` = Excellent · `F` = Fair · `P` = Poor

Example: `E`
