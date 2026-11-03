# Enrollment

Version 8.2.2 · TabID ENR

One record per continuous enrollment period, with medical/drug coverage flags, plan type, and payer type.

The Enrollment Table has a start/stop structure. Patients with medical coverage, drug coverage, or both should be included. A break in enrollment (of at least one day) or a change in either the medical or drug coverage variables should generate a new record.

**Sort order:** PatID, Enr_Start, Enr_End, MedCov, DrugCov, Chart

**Unique row:** PatID, Enr_Start, Enr_End, MedCov, DrugCov, Chart, PlanType, PayerType

## Variables

### `PatID`

Num(#) · missing not allowed

Unique patient identifier; an arbitrary person-level identifier used to link across tables. A new enrollment period generates a new record, but the same person keeps the same PatID on subsequent records. Choose the fewest bytes that hold all distinct values exactly: Num(3) holds up to 8,192; Num(4) up to 2,097,152; Num(5) up to 536,870,912; Num(6) up to 137,438,953,472; Num(7) up to 35,184,372,088,832; Num(8) up to 9,007,199,254,740,992.

PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source data partner. Analytical data sets use only the pseudo-identifier to link all information belonging to a patient.

Example: `123456789`

### `Enr_Start`

Num(4), SAS date · missing not allowed

Date of the beginning of the enrollment period. If the exact date is unknown, use the first day of the month. Enr_Start should not be before January 1, 2000.

Example: `1/1/2019`

### `Enr_End`

Num(4), SAS date · missing not allowed

Date of the end of the enrollment period. If the exact date is unknown, use the last day of the month. Do not impute Enr_End from the date of death in the Death table.

Example: `12/31/2019`

### `MedCov`

Char(1) · missing not allowed

Whether the health plan has any responsibility for covering medical care for the member during this enrollment period (that is, whether you expect to observe medical care provided to this member during the period). Mark `A` if only ambulatory visit coverage is provided.

Values: `Y` = Yes · `N` = No · `U` = Unknown · `A` = Ambulatory only

Example: `Y`

### `DrugCov`

Char(1) · missing not allowed

Whether the health plan has any responsibility for covering outpatient prescription drugs for the member during this enrollment period (that is, whether you expect to observe outpatient pharmacy dispensings for this member during the period).

Values: `Y` = Yes · `N` = No · `U` = Unknown

Example: `Y`

### `Chart`

Char(1) · missing not allowed

Chart abstraction flag answering "Are you able to request charts for this member?" It does not address chart availability. Mark `Y` if there are no contractual restrictions between you and the member (or sponsor) that would prohibit requesting any chart for this member.

Assign `N` when charts cannot be requested, including:

- Medicare members: all enrollment periods for Medicare members get `N`.
- Administrative services only (ASO) populations: all ASO enrollment periods get `N`.

If no definitive information indicates that charts cannot be requested, assign `Y`.

Values: `Y` = Yes · `N` = No

Example: `Y`

### `PlanType`

Char(1) · missing allowed

Type of insurance plan coverage for the patient during the enrollment span.

Values: `F` = Fee for service · `H` = HMO (Health Maintenance Organization) · `E` = EPO (Exclusive Provider Organization) · `P` = PPO (Preferred Provider Organization) · `S` = POS (Point of Service) · `M` = Other managed care · `O` = Other · `U` = Unknown

Example: `O`

### `PayerType`

Char(2) · missing allowed

Type of payer for the patient's plan during the enrollment span.

Values: `CO` = Commercial · `MC` = Medicare · `MD` = Medicaid · `OG` = Other government · `OT` = Other · `UN` = Unknown

Example: `CO`

## Collapsing enrollment spans

Adjacent and overlapping enrollment periods with the same PatID, Enr_Start, Enr_End, MedCov, DrugCov, and Chart values should be collapsed. Enrollment periods separated by more than one day should not be bridged: an Enr_End of 1/31/2019 bridges with an Enr_Start of 2/1/2019, but not with an Enr_Start of 2/2/2019.

When an overlapping span occurs because of PlanType or PayerType, set that variable to Other on both spans and collapse them into a single span.
