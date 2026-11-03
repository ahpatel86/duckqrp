# Vital Signs

SCDM 8.2.2 · TabID VIT

One record per vital signs result/entry.

The table contains one record per result/entry.

**Sort:** `PatID`, then `Measure_Date`.

**Unique row:** none defined for this table.

**Numeric lengths:** `Num(#)` variables take an as-needed length — use the fewest bytes that hold all distinct values. A numeric variable of a given byte length stores integers exactly up to: 3 bytes → 8,192; 4 → 2,097,152; 5 → 536,870,912; 6 → 137,438,953,472; 7 → 35,184,372,088,832; 8 → 9,007,199,254,740,992.

## Variables

### `PatID`
**Num(#)** (as-needed length) · Missing: never. Example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables.

`PatID` is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.

### `EncounterID`
**Num(#)** (as-needed length) · Missing: yes. Example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, Prescribing, and Patient-Reported Measures Response tables.

The Encounter, Diagnosis, Procedure, Inpatient Transfusion, Inpatient Pharmacy, Vital Signs, Mother-Infant Linkage, and Prescribing tables are linked by `PatID` and `EncounterID`.

### `Measure_Date`
**Num(4)** · Missing: never. Example: `12/1/2019`

Date the vital signs were measured. Values are SAS dates.

### `Measure_Time`
**Num(4)** · Missing: yes. Example: `10:33`

Time associated with the vital signs record, as a SAS time value (HH:MM). This may be the time an actual blood pressure measurement was taken, or a check-in time from the encounter.

### `HT`
**Num(8)** · Missing: yes; if missing, leave blank. Example: `60.50`

Height in inches, formatted `####.##`. Only populated if height was taken on this date. Values must be greater than 0.

### `WT`
**Num(8)** · Missing: yes; if missing, leave blank. Example: `170.25`

Weight in pounds, formatted `####.##`. Only populated if weight was taken on this date. Values must be greater than 0.

### `Diastolic`
**Num(3)** · Missing: yes; if missing, leave blank. Example: `70`

Diastolic blood pressure in mmHg, formatted `###`. Only populated if diastolic blood pressure was taken on this date. Values are integers greater than or equal to 0.

### `Systolic`
**Num(3)** · Missing: yes; if missing, leave blank. Example: `120`

Systolic blood pressure in mmHg, formatted `###`. Only populated if systolic blood pressure was taken on this date. Values are integers greater than or equal to 0.

### `BP_Type`
**Char(1)** · Missing: yes. Example: `E`

Type of blood pressure taken. Values:

- `E` — Extended
- `M` — Multiple
- `O` — Orthostatic
- `R` — Rooming

### `Position`
**Char(1)** · Missing: yes; if unknown, leave blank. Example: `1`

Position for orthostatic blood pressure. Values:

- `1` — Sitting
- `2` — Standing
- `3` — Supine

### `Tobacco`
**Char(1)** · Missing: yes; unknown values should be left blank. Example: `3`

Tobacco status as of the visit date. The "Not asked" value should be used only when it is a valid response from your system (e.g., it is a valid value for EPIC). The "Conflicting" value should be used when tobacco information from multiple sources disagrees.

Values:

- `1` — Current user
- `2` — Never
- `3` — Quit/former user
- `4` — Passive
- `5` — Environmental exposure
- `6` — Not asked
- `7` — Conflicting

### `Tobacco_Type`
**Char(1)** · Missing: yes; unknown values should be left blank. Example: `4`

Type of tobacco used. Values:

- `1` — Cigarettes only
- `2` — Other tobacco only
- `3` — Cigarettes and other tobacco
- `4` — None
