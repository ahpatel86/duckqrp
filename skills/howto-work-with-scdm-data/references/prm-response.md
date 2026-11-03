# Patient-Reported Measures Survey Response

SCDM 8.2.2 · TabID PRR

**Purpose:** Responses to surveys and questions in the PRM Survey table.

**Grain:** One record per response. The table does not contain a primary key because of occurrences of multiple surveys on the same date/time for a given person.

**Sort order:** PatID, then PRM_Code, then Response_Dt, then Response_Tm.

**Unique row:** None defined. No primary key exists; potential duplicates should be reviewed for inclusion in analysis.

## Variables

Numeric variables declared `Num(#)` use as-needed length: store each value in the fewest bytes that hold all distinct values. Integer capacity by byte length: 3 bytes hold integers up to 8,192; 4 bytes up to 2,097,152; 5 bytes up to 536,870,912; 6 bytes up to 137,438,953,472; 7 bytes up to 35,184,372,088,832; 8 bytes up to 9,007,199,254,740,992.

### `PatID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `123456789`

Unique patient identifier. Arbitrary person-level identifier used to link across tables. See note 2.

### `EncounterID`

`Num(#)` · length: fewest bytes that hold all distinct values · missing allowed · example: `98765432159753`

Unique encounter identifier. Arbitrary encounter-level identifier used to link across the Encounter, Diagnosis, Procedure, Vital Signs, Mother-Infant Linkage, Inpatient Pharmacy, Inpatient Transfusion, Prescribing, and Patient-Reported Measures Response tables. See note 3.

### `PRM_Code`

`Num(3)` · never missing · example: `13`

Unique integer identifier for a questionnaire type and subtype combination. Used to link across all PRM tables.

Values: 1–41. See the [PRM code reference](./prm-codes.md) for the survey instrument identified by each code.

### `SurveyID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `100001`

Unique identifier for a collection of questions administered as a single survey.

### `QuestionID`

`Num(3)` · never missing · example: `1001`

Unique identifier for a question. A question may have multiple question versions; see QuestionVerID.

### `QuestionVerID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `1`

Unique sequence number of the available versions of each question. If an edit is made to a question, the next published version carries a different sequence number than the first published version. All versions of each question that have been administered are included in the data. This may include questions updated because of spelling/grammatical errors as well as rewording to increase precision in earlier versions.

### `Response_Dt`

`Num(4)` · never missing · example: `12/22/2019`

Date associated with the entered response, stored as a SAS date value.

### `Response_Tm`

`Num(4)` · never missing · example: `18:35`

Time associated with the entered response, stored as a SAS time value.

### `Response_Text`

`Char(#)` · length: fewest bytes needed to contain all response values · never missing · example: `Several days`

Source response text.

### `Survey_Mode`

`Char(1)` · never missing · example: `2`

How the survey question was administered.

Values:

- `U` — Unknown
- `1` — Telephone/Video
- `2` — Office/Hospital Visit
- `3` — Home Visit
- `4` — Group Visit
- `5` — Mail
- `6` — Web/Electronic
- `7` — Other

## Notes

1. This table does not contain a primary key due to occurrences of multiple surveys on the same date/time for a given person. Potential duplicates should be reviewed for inclusion in analysis.
2. PatID is a pseudo-identifier with a consistent crosswalk to the true identifier retained by the source Data Partner. For analytical data sets requiring patient-level data, only the pseudo-identifier is used to link across all information belonging to a patient.
3. The Encounter, Diagnosis, Procedure, Inpatient Transfusion, Inpatient Pharmacy, Vital Signs, Mother-Infant Linkage, Prescribing, and Patient-Reported Measures Response tables are linked by PatID and EncounterID.
