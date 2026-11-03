# Dispensing

Version 8.2.2 · TabID DIS

One record per unique outpatient pharmacy dispensing.

Rollback transactions and other adjustments should be processed before populating this table. This processing may be handled differently by Data Partners and may be affected by billing cycles; transactions indicative of a dispensing being canceled or not picked up by the member should be resolved first.

Medications distributed in other settings — such as infusions given in medical practices or inpatient hospitals — are captured in the utilization tables. Medication prescriptions (as opposed to dispensings) are captured in the Prescribing table.

**Sort order:** PatID, RxDate

**Unique row:** PatID, RxDate, Rx_CodeType, Rx, ProviderID

**Num(#) lengths:** choose the fewest bytes that hold all distinct values exactly — Num(3) holds up to 8,192; Num(4) up to 2,097,152; Num(5) up to 536,870,912; Num(6) up to 137,438,953,472; Num(7) up to 35,184,372,088,832; Num(8) up to 9,007,199,254,740,992.

## Variables

### `PatID`

Num(#) · missing not allowed

Unique patient identifier; an arbitrary person-level identifier used to link across tables. Choose the fewest bytes that hold all distinct values exactly (see Num(#) lengths above).

PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source data partner. Analytical data sets use only the pseudo-identifier to link all information belonging to a patient.

Example: `123456789`

### `ProviderID`

Num(#) · SAS7BDAT: ordinary missing not allowed; `.U` allowed

SAS7BDAT only: special missing values become ordinary nulls in Parquet.

Unique provider identifier for the individual provider who prescribed the drug. Like PatID, it is a pseudo-identifier with a consistent crosswalk to the real identifier. If an individual provider/prescriber cannot be identified, set to `.U`. Links to the Provider table. Choose the fewest bytes that hold all distinct values exactly (see Num(#) lengths above).

If all ProviderID values are unknown (all `.U`), use Num(3). If the ProviderID length is greater than 3 in any other table, set the length to the maximum length found across all tables; variable lengths must be consistent across tables for same-named variables.

Example: `12345`

### `RxDate`

Num(4), SAS date · missing not allowed

Dispensing date (as close as possible to the date the person received the dispensing).

Example: `11/29/2005`

### `Rx`

Char(#) · missing not allowed

Drug code representing a prescribed medication or medical device. Expunge any placeholders (for example `-` or an extra digit). Use the fewest bytes needed to contain values for all values of Rx_CodeType.

Example: `00006007431`

### `Rx_CodeType`

Char(2) · missing not allowed

Code type of the prescribed medication or medical device. Combined with Rx, this field captures any type of prescribed medication or medical device available in the source data. Other code types may be added as new terminologies are used. The listed values cover medication and medical device codes used in different international jurisdictions.

Values: `ND` = National Drug Code (US) · `SN` = SNOMED CT (US) · `SK` = SNOMED CT (UK/CPRD) · `DM` = Dictionary of Medicines and Devices (UK) · `DI` = Drug Identification Number (CA) · `RN` = RxNorm (US) · `AT` = Anatomical Therapeutic Chemical Classification (DK)

Example: `SK`

### `RxSup`

Num(4) · missing allowed

Days supply: the number of days the medication supports based on the number of doses as reported by the pharmacist. Typically found on the dispensing record; it should not be necessary to calculate this variable for the SCDM. Positive integer values are expected.

Example: `30`

### `RxAmt`

Num(4) · missing allowed

Amount dispensed: number of units (pills, tablets, vials) dispensed, net amount per dispensing. Typically found on the dispensing record; it should not be necessary to calculate this variable for the SCDM. Positive values are expected.

Example: `60`
