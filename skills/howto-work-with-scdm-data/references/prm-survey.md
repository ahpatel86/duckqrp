# Patient-Reported Measures Survey

SCDM 8.2.2 · TabID PRS

**Purpose:** Site-specific surveys and questions administered to patients.

**Grain:** One record per survey question administered. Data in this table is site-specific, so questions may not be consistent across sites. Surveys are included only if they have been administered at least one time.

**Sort order:** PRM_Code, then SurveyID, then QuestionID, then QuestionVerID.

**Unique row:** PRM_Code, SurveyID, QuestionID, QuestionVerID.

## Variables

Numeric variables declared `Num(#)` use as-needed length: store each value in the fewest bytes that hold all distinct values. Integer capacity by byte length: 3 bytes hold integers up to 8,192; 4 bytes up to 2,097,152; 5 bytes up to 536,870,912; 6 bytes up to 137,438,953,472; 7 bytes up to 35,184,372,088,832; 8 bytes up to 9,007,199,254,740,992.

### `PRM_Code`

`Num(3)` · never missing · example: `13`

Unique integer identifier for a questionnaire type and subtype combination. Used to link across all PRM tables.

Values: 1–41. See the [PRM code reference](./prm-codes.md) for the survey instrument identified by each code.

### `SurveyID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `100001`

Unique identifier for a collection of questions administered as a single survey. Site-specific.

### `Survey_Name`

`Char(#)` · length: site-specific, as needed · never missing · example: `DPXX_PHQ_PROJECT_ABC`

Survey name associated with a specific SurveyID. Multiple survey names may share the same SurveyID. Site-specific.

### `Survey_Data_Source`

`Char(2)` · never missing · example: `FL`

Data source of the survey. For site-specific surveys that do not fall into one of the 3 primary categories, set the value to LO. Useful for mapping back to source data.

Values:

- `FL` — flowsheet
- `LO` — local
- `SF` — smart form
- `QU` — questionnaire

### `QuestionID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `1002`

Unique identifier for a question. Site-specific. A question may have multiple question versions; see QuestionVerID.

### `QuestionVerID`

`Num(#)` · length: fewest bytes that hold all distinct values · never missing · example: `1`

Unique sequence number of the available versions of each question. Site-specific. If an edit is made to a question, the next published version carries a different sequence number than the first published version. All versions of each question that have been administered are included in the data. This may include questions updated because of spelling/grammatical errors as well as rewording to increase precision in earlier versions.

### `Question_Dt`

`Num(4)` · never missing · example: `12/31/2015`

Date of publication, stored as a SAS date. Each combination of QuestionID/QuestionVerID has a unique date of publication.

### `Question_Text`

`Char(#)` · length: site-specific, as needed · never missing · example: `Over the last 2 weeks, how often have you been bothered by any of the following problems? Little interest or pleasure in doing things?`

Source question text. If there is a response to the question in the data, the question is included.

## Notes

This table and the PRM Response table are linked to see the full questions and the patient-provided responses. They are linked on PRM_Code, SurveyID, QuestionID, QuestionVerID.
