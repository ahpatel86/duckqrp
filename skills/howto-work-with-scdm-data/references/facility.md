# Facility

Version 8.2.2 · TabID FAC

One record per facility identifier referenced by other tables.

Should capture all uniquely recorded facility identifiers in the Encounter table and the Laboratory Results (if populated) tables.

**Sort order:** FacilityID

**Unique row:** FacilityID

## Variables

### `FacilityID`

Num(#) · SAS7BDAT: ordinary missing not allowed; `.U` allowed

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Servicing facility identifier: a local facility pseudo-identifier that identifies a hospital or clinic. Taken from facility claims. Used for chart abstraction and validation. Choose the fewest bytes that hold all distinct values exactly: Num(3) holds up to 8,192; Num(4) up to 2,097,152; Num(5) up to 536,870,912; Num(6) up to 137,438,953,472; Num(7) up to 35,184,372,088,832; Num(8) up to 9,007,199,254,740,992.

FacilityID links to the same-named variable in the Encounter and Laboratory Results tables and must be the same length across those tables.

If ALL FacilityID values in the other tables are `.U`, still create a Facility table that meets these requirements:

- Only 1 row in the table
- FacilityID has a length of Num(3) (holds up to 8,192)
- FacilityID is set to `.U`
- Facility_Location has a length of Char(1)
- Facility_Location is set to `" "` (blank)

Example: `12345678`

### `Facility_Location`

Char(#) · missing allowed

Geographic location (postal/region code) of the servicing facility (not the billing location). Should be left blank if missing.

Example: `04090`
