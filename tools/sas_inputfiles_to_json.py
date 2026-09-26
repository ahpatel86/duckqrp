"""Convert a SAS QRP input-file set (sas7bdat) to the JSON this package reads.

    python tools/sas_inputfiles_to_json.py <inputfiles_dir> study.json

Follows the QRP_PARAMETERS indirection: that table names the dataset
for each role (cohortfile -> wp307_cohort, and so on), so the role names
are stable even though the dataset names are study-specific.

Verified on a real 40-cohort study: 12 tables, 200,480 cohort codes,
103,503 risk-score codes. Needs `pyreadstat`.
"""
import json, sys, math
from pathlib import Path
import pyreadstat

src = Path(sys.argv[1]); dest = Path(sys.argv[2])

params_df, _ = pyreadstat.read_sas7bdat(str(src / "qrp_parameters.sas7bdat"))
# qrp_parameters is a two-column parameter/value table
pcol, vcol = params_df.columns[0], params_df.columns[1]
params = {str(r[pcol]).strip().lower(): str(r[vcol]).strip()
          for _, r in params_df.iterrows()}

def clean(v):
    if v is None: return None
    if isinstance(v, float):
        if math.isnan(v): return None
        return int(v) if v.is_integer() else v
    s = str(v).strip()
    return s or None

def rows(name):
    f = src / f"{name}.sas7bdat"
    if not f.exists(): return []
    df, _ = pyreadstat.read_sas7bdat(str(f))
    return [{c.lower(): clean(v) for c, v in r.items()}
            for _, r in df.iterrows()]

# QRP_PARAMETERS names the table for each role; follow the indirection
ROLES = ("cohortfile", "type2file", "cohortcodes", "inclusioncodes",
         "userstrata", "covariatecodes", "mfufile", "utilfile",
         "riskscorefile", "riskscorecodes", "drugclassfile",
         "monitoringfile", "zipfile", "labcodes", "codestrength")
out = {"qrp_parameters_scalars": params}
for role in ROLES:
    table = params.get(role)
    if not table: continue
    data = rows(table)
    if data:
        out[role] = data
        print(f"  {role:<16} <- {table:<22}{len(data):>7} rows")

dest.write_text(json.dumps(out))
print(f"  wrote {dest} ({dest.stat().st_size/1e6:.1f} MB)")
