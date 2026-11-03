# Demographic

Version 8.2.2 · TabID DEM

One record per patient with the most recent birth date, sex, race, ethnicity, and postal code.

Holds the most recent Birth_Date, Sex, Race, PostalCode, ImputedRace, ImputedHispanic, and Hispanic for each PatID.

**Sort order:** PatID

**Unique row:** PatID

## Variables

### `PatID`

Num(#) · missing not allowed

Unique patient identifier; an arbitrary person-level identifier used to link across tables. Choose the fewest bytes that hold all distinct values exactly: Num(3) holds up to 8,192; Num(4) up to 2,097,152; Num(5) up to 536,870,912; Num(6) up to 137,438,953,472; Num(7) up to 35,184,372,088,832; Num(8) up to 9,007,199,254,740,992.

PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source data partner. Analytical data sets use only the pseudo-identifier to link all information belonging to a patient.

Example: `123456789`

### `Birth_Date`

Num(4), SAS date · missing allowed

Date of birth.

Example: `12/5/1971`

### `Sex`

Char(1) · missing not allowed

Sex.

Values: `A` = Ambiguous · `F` = Female · `M` = Male · `U` = Unknown

Example: `F`

### `Hispanic`

Char(1) · missing not allowed

Hispanic status: a person of Cuban, Mexican, Puerto Rican, South or Central American, or other Spanish culture or origin, regardless of race.

Values: `N` = No · `Y` = Yes · `U` = Unknown

Example: `N`

### `Race`

Char(1) · missing not allowed

Race. Use only one race value per patient. At this time the SCDM retains one Race and one Hispanic value per individual.

Values:

- `0` = Unknown
- `1` = American Indian or Alaska Native (a person having origins in any of the original peoples of North and South America (including Central America), and who maintains tribal affiliation or community attachment)
- `2` = Asian (a person having origins in any of the original peoples of the Far East, Southeast Asia, or the Indian subcontinent including, for example, Cambodia, China, India, Japan, Korea, Malaysia, Pakistan, the Philippine Islands, Thailand, and Vietnam)
- `3` = Black or African American (a person having origins in any of the black racial groups of Africa)
- `4` = Native Hawaiian or Other Pacific Islander (a person having origins in any of the original peoples of Hawaii, Guam, Samoa, or other Pacific Islands)
- `5` = White (a person having origins in any of the original peoples of Europe, the Middle East, or North Africa)
- `M` = Multi-racial (a person who identifies with two or more races)

Example: `2`

### `PostalCode`

Char(#) · missing allowed

Postal/region code. USA: first 5 digits of the ZIP code of the patient's most recent primary residence (5 characters only). Other countries: complete postal code or region identifier (variable length depends on code length).

Example: `04090`

### `PostalCode_Date`

Num(4), SAS date · missing allowed

Earliest date that the PostalCode is believed to be continuously correct, up until the end date of your source data for the ETL. The date is updated/overwritten as the postal code for a patient/member changes over time.

Example: `12/12/2009`

### `ImputedRace`

Char(1) · missing allowed

Imputed race, using the same code values as Race (`0`–`5`, `M`; full definitions under `Race` above).

Example: `2`

### `ImputedHispanic`

Char(1) · missing allowed

Imputed Hispanic status; same values and definition as Hispanic.

Values: `N` = No · `Y` = Yes · `U` = Unknown

Example: `N`

## Imputation guidance

Data Partners who do not impute a value for Race or Hispanic status should leave ImputedRace or ImputedHispanic missing. Data Partners may populate ImputedRace or ImputedHispanic regardless of the value of Race or Hispanic.
