"""
Typed configuration for the QRP Type 2 pipeline.

Design note
-----------
The SAS package carries study parameters as macro variables, and the
PySpark port carried them as strings that look like macro variables
(`sex = '"F" "M" "U" "A"'`, `agestrat = "00-01 02-04 65+"`), re-parsed
at every use, with data probes (`df.limit(1).count() > 0`) deciding
which branch to take *per cohort iteration*.

Here the input JSON is parsed exactly once, at startup, into frozen
dataclasses. Two consequences:

1. Every "should this branch run?" question is answered in Python
   against config, before any SQL executes. No query is ever run to
   decide whether to run a query.
2. The resolved config is then registered *as DuckDB tables* (see
   `register()`), so the SQL joins to it instead of being built by
   string interpolation. That is what lets all cohorts run in one
   pass rather than in a Python loop.
"""

from __future__ import annotations

import re as _re
from math import isfinite as _isfinite

from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Sequence, overload

# --------------------------------------------------------------------
# Age strata
# --------------------------------------------------------------------

_UNIT_SUFFIX = {"y": "years", "m": "months", "d": "days", "w": "weeks"}


@dataclass(frozen=True)
class AgeStratum:
    ordinal: int
    label: str
    lo: int
    hi: int          # 99999 for an open-ended final stratum ("75+")
    unit: str        # years | months | days | weeks


@dataclass(frozen=True)
class AgeStrata:
    strata: tuple[AgeStratum, ...]

    @classmethod
    def parse(cls, spec: str | None) -> "AgeStrata":
        """Parse the SAS agestrat string. Done once, here, not per row.

        Accepts tokens like '00-01', '65+', '18-44y', '06-11m'.
        """
        spec = (spec or "").strip() or (
            "00-01 02-04 05-09 10-14 15-18 19-21 22-44 45-64 65-74 75+"
        )
        out: list[AgeStratum] = []
        for i, tok in enumerate(spec.split(), start=1):
            raw = tok.strip()
            unit = "years"
            if raw and raw[-1].lower() in _UNIT_SUFFIX:
                unit = _UNIT_SUFFIX[raw[-1].lower()]
                raw = raw[:-1]
            if raw.endswith("+"):
                lo, hi = int(raw[:-1]), 99999
            elif "-" in raw:
                a, b = raw.split("-", 1)
                lo, hi = int(a), int(b)
            else:
                lo = hi = int(raw)
            out.append(AgeStratum(i, tok, lo, hi, unit))
        if not out:
            raise ValueError(f"could not parse agestrat spec: {spec!r}")
        return cls(tuple(out))


# --------------------------------------------------------------------
# Per-cohort study parameters
# --------------------------------------------------------------------


# Care setting / principal-diagnosis restriction on a code.
#
# `caresettingprincipal` packs one or more 3-character tokens into a
# single string: two characters of EncType plus one of PDX. SAS expands
# it in ms_caresettingprincipal.sas and then matches with
#     (EncType = '**' OR EncType = claim.enctype)
# AND (Pdx     = '*'  OR Pdx     = claim.pdx)
#
# Encoding, from the macro:
#   '*' is the wildcard, written 'A' after SAS's translate()
#   '.' means missing, written '_'
#   'AAA' (i.e. '***') or an empty value means "all care settings"
CARE_SETTING_ALL = ("**", "*")


def parse_care_setting(value: Any) -> tuple[tuple[str, str], ...]:
    """Expand a caresettingprincipal string into (enctype, pdx) pairs.

    Returns (('**', '*'),) — match anything — for an empty value or an
    explicit '***', which is what SAS does.
    """
    raw = str(value or "").strip().upper()
    # SAS: translate(var, 'A', '*', '_', '.') — '*'->'A' and '.'->'_'
    raw = raw.replace("*", "A").replace(".", "_")
    if not raw or "AAA" in raw.split():
        return (CARE_SETTING_ALL,)

    pairs: list[tuple[str, str]] = []
    for token in raw.split():
        # tokens are fixed 3-character groups, possibly concatenated
        for i in range(0, len(token) - 2, 3):
            chunk = token[i:i + 3]
            if len(chunk) < 3:
                continue
            enctype = chunk[:2]
            pdx = chunk[2]
            if enctype == "AA":
                enctype = "**"       # wildcard care setting
            if pdx == "_":
                pdx = ""             # missing PDX
            elif pdx == "A":
                pdx = "*"            # wildcard PDX
            pairs.append((enctype, pdx))
    return tuple(pairs) or (CARE_SETTING_ALL,)


def parse_lab_result(spec: Any) -> tuple[str | None, float | None, float | None]:
    """Parse a LABRESULT criterion string into (operator, lo, hi).

    ms_extractlabs.sas:133-172 accepts '<=', '<', '>=', '>', '~=' and a
    range written with ':' — deliberately not '-', because SAS notes a
    hyphen is ambiguous with a negative lower bound.

    Order matters: '<=' must be tested before '<', or '<=7' parses as
    '<' with a bound of '=7'.
    """
    raw = str(spec or "").strip()
    if not raw:
        return (None, None, None)

    def _bad(why: str) -> tuple[None, None, None]:
        # A criterion that cannot be parsed is DROPPED, which makes the
        # extraction broader than intended — the dangerous direction,
        # and the same reason unsupported inclusion rules warn. Silence
        # here meant a typo in a lab threshold quietly removed the
        # threshold. Reported in review.
        import warnings as _w
        _w.warn(
            f"labresult {raw!r} {why}; the criterion is IGNORED, so this "
            f"lab code extracts more records than the study intends",
            stacklevel=3,
        )
        return (None, None, None)

    if ":" in raw:
        lo_s, _, hi_s = raw.partition(":")
        try:
            lo, hi = float(lo_s.strip()), float(hi_s.strip())
        except ValueError:
            return _bad("is not a numeric range")
        if not (_isfinite(lo) and _isfinite(hi)):
            return _bad("has a non-finite bound")
        if lo > hi:
            # matches nothing at all, silently
            return _bad(f"is an inverted range ({lo} > {hi})")
        return (":", lo, hi)
    for op in ("<=", ">=", "~=", "<", ">", "="):   # two-char first
        if raw.startswith(op):
            try:
                bound = float(raw[len(op):].strip())
            except ValueError:
                return _bad(f"has no numeric bound after {op!r}")
            if not _isfinite(bound):
                return _bad("has a non-finite bound")
            return (op, bound, None)
    try:
        bound = float(raw)
    except ValueError:
        return _bad("is not a recognised comparison")
    if not _isfinite(bound):
        return _bad("has a non-finite bound")
    return ("=", bound, None)


# Combo covariates (codecat='CC') are BOOLEAN EXPRESSIONS over other
# covariate numbers, not code lists. From a real input file:
#
#   covar 12: 3 or 4 or 5 or 6
#   covar 14: 2 and (3 or 4 or 5 or 6)
#   covar 49: not (1 or 48)
#
# The grammar is small and closed: integers, `and`, `or`, `not`, and
# parentheses. It is parsed here rather than evaluated as SQL text so
# that a malformed expression fails at load with a message, and so no
# study-supplied string is ever concatenated into a query.
_COMBO_TOKEN = _re.compile(r"\s*(\(|\)|and\b|or\b|not\b|\d+)", _re.I)


_SAFE_RUNID = _re.compile(r"[^A-Za-z0-9_-]")


def safe_run_id(value: Any) -> str:
    """Reduce a run id to characters that cannot alter a path.

    `run_id` is interpolated straight into output filenames and into the
    run-log filename. A value like `../../../tmp/escaped` resolves
    outside the output directory entirely — and past the dplocal/msoc
    split, which is the disclosure boundary, not a naming convention.
    So patient-level output could be written somewhere it was never
    meant to go, by a study parameter. Reported in review.

    Anything outside `[A-Za-z0-9_-]` becomes an underscore. Empty or
    all-separator values fall back to "qrp" rather than producing a
    filename that starts with the table name.
    """
    cleaned = _SAFE_RUNID.sub("_", str(value or "")).strip("_-")
    return cleaned or "qrp"


def parse_combo(expr: Any) -> tuple[str, tuple[int, ...]]:
    """Parse a combo expression into (SQL template, referenced covarnums).

    The template uses `{N}` placeholders for each referenced covariate,
    which the SQL stage substitutes with a has-covariate test. Raises
    ValueError on anything outside the grammar.
    """
    raw = str(expr or "").strip()
    if not raw:
        raise ValueError("empty combo expression")

    out: list[str] = []
    refs: list[int] = []
    pos = 0
    depth = 0
    while pos < len(raw):
        m = _COMBO_TOKEN.match(raw, pos)
        if not m:
            raise ValueError(
                f"combo expression {raw!r}: cannot parse at {raw[pos:][:20]!r}"
            )
        tok = m.group(1)
        low = tok.lower()
        if low == "(":
            depth += 1
            out.append("(")
        elif low == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(f"combo expression {raw!r}: unbalanced ')'")
            out.append(")")
        elif low in ("and", "or", "not"):
            out.append(f" {low.upper()} ")
        else:
            n = int(tok)
            refs.append(n)
            # named, not positional: `{3}` is a POSITIONAL placeholder
            # to str.format and raises IndexError.
            out.append("{c%d}" % n)
        pos = m.end()
    if depth:
        raise ValueError(f"combo expression {raw!r}: unbalanced '('")
    if not refs:
        raise ValueError(f"combo expression {raw!r}: references no covariate")
    return "".join(out), tuple(dict.fromkeys(refs))


def _codes(value: Any, default: Sequence[str]) -> tuple[str, ...]:
    """Normalise a demographic code list.

    Accepts a real list, or the SAS-style space-delimited quoted string
    the PySpark port passed around. Empty means 'all', i.e. the default.
    """
    if value is None:
        return tuple(default)
    if isinstance(value, (list, tuple)):
        vals = [str(v).strip().upper() for v in value if str(v).strip()]
    else:
        vals = [v.strip().strip('"').upper() for v in str(value).split()]
        vals = [v for v in vals if v]
    return tuple(vals) if vals else tuple(default)


@dataclass(frozen=True)
class CohortConfig:
    """Everything needed to evaluate one cohort group.

    All validation happens in `validate()` at load time, so an invalid
    study fails in milliseconds instead of after a partial run.
    """

    cohortgrp: str

    # --- enrollment -------------------------------------------------
    enrollment_num: int = 1
    coverage: str = "MD"
    enrol_gap: int = 0
    chart_required: bool = False
    enr_days: int = 183

    # --- demographics ----------------------------------------------
    sex: tuple[str, ...] = ("F", "M", "U", "A")
    race: tuple[str, ...] = ("0", "1", "2", "3", "4", "5", "M")
    hispanic: tuple[str, ...] = ("Y", "N", "U")
    age_strata: AgeStrata = field(default_factory=lambda: AgeStrata.parse(None))

    # --- exposure / index -------------------------------------------
    wash_per: int | None = 183
    point: bool = False
    episode_gap: int | None = 0
    episode_gap_type: str = "F"        # F = fixed days, P = % of prior supply
    exp_ext_per: int = 0
    min_epis_dur: int = 1
    max_epis_dur: int = 0              # 0 = unbounded
    min_days_supp: int = 0
    at_risk_start: int = 0
    blackout_per: int = 0

    # --- follow-up ---------------------------------------------------
    # None means "never had an event" — the STRICTEST setting, not the
    # loosest. ms_createpov56.sas:78 is explicit: "If FupWashPer=. then
    # patients need to never have had an Event (hence 99999)". Parsing a
    # missing value as 0 meant no washout at all, keeping patients SAS
    # would drop.
    #
    # Represented as None rather than SAS's 99999 sentinel because the
    # `fup_wash_per > enr_days` validation applies to the value the study
    # SUPPLIED; a sentinel would fail that check spuriously.
    fup_wash_per: int | None = 0
    event_count: int = 0               # 0 none, 1 dedup by code, 2 first per day
    req_days_aft_ind: int = 0
    req_days_aft_epi: int = 0
    censor_death: bool = True

    # --- dose --------------------------------------------------------
    min_cum_dose: float | None = None
    max_cum_dose: float | None = None
    cum_dose_per: int | None = None
    min_cfdd: float | None = None
    max_cfdd: float | None = None

    # --- codes --------------------------------------------------------
    code_supply: int | None = None
    # OUTPUTDENOM: whether this cohort's denominator is computed.
    #   "Y"  members and member-days
    #   "M"  members only — DenNumMemDays is blanked
    #        (ms_cidadenom.sas:1347)
    #   "N"  no denominator at all
    # SAS gates the whole denominator stage on it
    # (ms_cidadenom.sas:113-115), so ignoring it produced denominators
    # for a study that asked for none.
    output_denom: str = "Y"
    # (code, codecat) pairs. codecat is one of RX / PX / DX and decides
    # which claim domain the code is extracted from.
    # (code, codecat, codetype, code_supply).
    #
    # codetype is the CODE SYSTEM — ICD-10 ("10"), HCPCS ("HC"), NDC
    # ("ND") and so on. One cohort's codes routinely span several: the
    # real study seen has DX/10, PX/10, PX/HC, PX/ND and RX/ND under a
    # single cohort. The same code STRING can exist in two systems, so
    # matching on (code, codecat) alone over-matches — it found 1,171
    # members where SAS found 1,113.
    #
    # code_supply overrides the claim's
    # RxSup when set; None means use the claim's own value.
    exposure_codes: tuple[tuple[str, str, str, int | None], ...] = ()
    event_codes: tuple[tuple[str, str, str, int | None], ...] = ()
    # fupcriteria='IOC' codes: the follow-up washout is evaluated
    # against these as well as against the event codes
    # (ms_cidanum.sas:1664 -> _FUPWash, consumed by
    # _WashEventsInFupWash in ms_createpov56.sas).
    ioc_codes: tuple[tuple[str, str, str, int | None], ...] = ()
    # FUT: a claim inside an episode truncates it to that date.
    trunc_codes: tuple[tuple[str, str, str, int | None], ...] = ()
    # (code, stockgroup) for DEF codes. SAS stockpiles WITHIN a
    # stockgroup (ms_stockpiling.sas passes GROUPING=StockGroup ...), so
    # two drugs in one cohort are pushed forward independently. Absent a
    # stockgroup, every code shares one, which reproduces the previous
    # behaviour for single-drug cohorts.
    exposure_stockgroups: tuple[tuple[str, str], ...] = ()
    # (code, enctype, pdx) for EVENT codes carrying a care-setting
    # restriction. Parsed but silently dropped before — a study could
    # specify it, get no warning, and receive a broader cohort than SAS.
    event_care_settings: tuple[tuple[str, str, str], ...] = ()

    # ---------------- derived flags (no data probe needed) -----------

    @property
    def needs_dose(self) -> bool:
        """Whether the dose restriction path runs at all.

        In the PySpark version this was six `.limit(1).count()` probes
        per cohort iteration against small config tables. It is a pure
        function of config.
        """
        return (
            self.min_cum_dose is not None
            or (self.max_cum_dose is not None and self.cum_dose_per is not None)
            or self.min_cfdd is not None
            or self.max_cfdd is not None
        )

    def validate(self) -> None:
        """SAS cross-parameter rules, enforced up front."""
        errs: list[str] = []
        if self.wash_per is not None and self.wash_per > self.enr_days:
            errs.append(f"wash_per ({self.wash_per}) > enr_days ({self.enr_days})")
        if self.fup_wash_per is not None and self.fup_wash_per > self.enr_days:
            errs.append(
                f"fup_wash_per ({self.fup_wash_per}) > enr_days ({self.enr_days})"
            )
        if self.max_epis_dur and self.at_risk_start > self.max_epis_dur:
            errs.append("at_risk_start cannot exceed max_epis_dur")
        if self.blackout_per and self.at_risk_start:
            errs.append("blackout_per and at_risk_start are mutually exclusive")
        if self.point:
            conflicting = {
                "episode_gap": self.episode_gap,
                "exp_ext_per": self.exp_ext_per,
                "min_epis_dur": self.min_epis_dur if self.min_epis_dur != 1 else None,
                "min_days_supp": self.min_days_supp,
                "blackout_per": self.blackout_per,
                "req_days_aft_epi": self.req_days_aft_epi,
                "code_supply": self.code_supply,
            }
            bad = [k for k, v in conflicting.items() if v]
            if bad:
                errs.append(f"point=Y forbids: {', '.join(sorted(bad))}")
        # A minimum PRIOR cumulative dose is unsatisfiable when the
        # lookback sits inside the washout: washout guarantees there are
        # no qualifying claims in that window, so prior dose is always 0
        # and every index date is excluded. Caught here because the
        # symptom — an empty cohort several stages later — is a miserable
        # thing to debug. Found by running a study that did exactly this.
        if (
            self.min_cum_dose is not None
            and self.cum_dose_per is not None
            and self.wash_per is not None
            and self.cum_dose_per <= self.wash_per
        ):
            errs.append(
                f"min_cum_dose with cum_dose_per ({self.cum_dose_per}) "
                f"<= wash_per ({self.wash_per}) excludes every index date: "
                f"washout guarantees no prior claims in that window"
            )
        # Checked across ALL exposure codes, not just the first. This
        # used to read `supply_rows[0]`, collapsing a per-CODE value to
        # one per cohort: a study where only the third code set
        # CODESUPPLY passed validation, and a study where only the first
        # did failed it. CODESUPPLY is per code — 150 of 1,124 rows
        # carry it in the real study file.
        if any(sup is not None for _, _, _, sup in self.exposure_codes) and (
            self.min_cfdd is not None or self.max_cfdd is not None
        ):
            errs.append(
                "CODESUPPLY must be unset when CFDD limits are used: "
                "CFDD is dose per day of supply, so overriding the "
                "supply changes the quantity the limit is applied to")
        if self.coverage.upper() not in {"MD", "M", "D"}:
            errs.append(f"coverage must be MD, M or D (got {self.coverage!r})")
        if errs:
            raise ValueError(
                f"cohort {self.cohortgrp!r} has invalid configuration:\n  - "
                + "\n  - ".join(errs)
            )


@dataclass(frozen=True)
class Covariate:
    """One baseline covariate definition.

    covfrom / covto are day offsets from the index date. A NULL bound in
    the source means unbounded; it is resolved to a sentinel HERE rather
    than with a COALESCE inside the join predicate, which is what keeps
    the predicate simple enough for DuckDB to push down.
    """

    covarnum: int
    covarname: str
    codecat: str                 # DX | RX
    covfrom: int = -365
    covto: int = -1
    # Each end of the window anchors independently, exactly as for
    # inclusion rules (ms_cidacov.sas:47-54). Blank means INDEXDT.
    #   INDEXDT       the index date
    #   EPISODEENDDT  the episode end — a forward-looking window
    #   INDEXDT_EXP   indexdt_exp — when exposure began in a pregnancy exposure window (Type 4)
    covfromanchor: str = "INDEXDT"
    covtoanchor: str = "INDEXDT"
    dateonly: bool = False
    # Combo covariates (codecat='CC'): a boolean expression over other
    # covariate numbers, parsed at load. `combo_sql` is a template with
    # {N} placeholders; `combo_refs` are the covarnums it needs.
    combo_sql: str = ""
    combo_refs: tuple[int, ...] = ()
    codes: tuple[str, ...] = ()

    UNBOUNDED_BEFORE = -999999
    UNBOUNDED_AFTER = 999999


@dataclass(frozen=True)
class InclusionRule:
    """One row of the INCLUSIONCODES file.

    THREE levels of nesting, not two.

    `ms_processinputfiles.sas:715-740` derives two numeric variables from
    the character columns in the input file:

        cond     renumbered per distinct CONDLEVEL
        subcond  renumbered per distinct SUBCONDLEVEL *within* a condlevel

    and `ms_createpov3.sas:22-38` gives the combining rules:

        codes within a subcondition   OR   (any one satisfies it)
        subconditions within a cond   AND  ("If all subconditions are
                                             satisfied, then condition is
                                             satisfied")
        conditions                    AND  (every condition must pass)

    `subcondinclusion` inverts a subcondition: "If the subcondition is
    met but it is a subexclusion, then means that condition not
    satisfied."

    Note `cond` and `subcond` are DERIVED, not input columns — the file
    carries `condlevel` and `subcondlevel` as character values. Reading
    a `cond` column that does not exist gives every rule cond=1, which
    collapses every condition into one and ORs what should be ANDed.

    Other fields:
      indexcriteria INC / EXC (index-anchored), IEV / EEV (event-anchored)
      condfrom/to   day offsets from the anchor
      codedays      minimum number of DISTINCT days carrying the code
      minrxdays     RX only: minimum total DAYS OF SUPPLY in the window
      codecat       DX | RX | PX
    """

    cohortgrp: str
    cond: int
    # Defaults so a rule can be built directly in a test without
    # restating the derivation; load_study_dict always supplies them.
    # 'INC' is SAS's default when indexcriteria is absent.
    criteria: str = "INC"        # INC | EXC | IEV | EEV
    subcond: int = 1
    condlevel: int = 1
    codecat: str = "DX"
    condfrom: int = -365
    condto: int = -1
    # Each END of the window anchors independently
    # (ms_createpov3.sas:139-175). Blank means INDEXDT.
    #   INDEXDT       the index date
    #   EPISODEENDDT  the episode end — a forward-looking window
    #   INDEXDT_EXP   indexdt_exp — when exposure began in a pregnancy exposure window (Type 4)
    condfromanchor: str = "INDEXDT"
    condtoanchor: str = "INDEXDT"
    codedays: int = 1
    # RX only: total DAYS OF SUPPLY in the window must reach this, not
    # the number of claims (ms_createpov3.sas:26 — "total days in window
    # >= minrxdays"). Defaults to 1, which any dispensing satisfies.
    minrxdays: int = 1
    # Per-SUBCONDITION dose thresholds (ms_createpov3.sas:333-353).
    # SAS aggregates them across the rows of a subcondition with
    # max(mincumdose), min(minafdd), max(maxafdd) — strictest lower
    # bound, widest upper bound.
    #   mincumdose  total cumdose in the window >= this
    #   minafdd/maxafdd  average filled daily dose, computed as
    #       round(sum(cfdd) / sum(numdispensing), 1)   (line 452)
    mincumdose: float | None = None
    minafdd: float | None = None
    maxafdd: float | None = None
    subcondlevel: str = ""
    # A sub-EXCLUSION: meeting it makes the condition fail.
    subcond_inclusion: bool = True
    codes: tuple[str, ...] = ()

    @property
    def is_exclusion(self) -> bool:
        return self.criteria in ("EXC", "EEV")

    @property
    def is_event_anchored(self) -> bool:
        return self.criteria in ("IEV", "EEV")


@dataclass(frozen=True)
class RiskScoreCode:
    """One row of the RISKSCORECODES lookup.

    A risk score is a weighted sum over CONDITIONS, not over claims:
    `ms_computeriskscores.sas:369` takes `max(weight)` per
    `(patient, indexdt, condidnum)` before summing, so meeting a
    condition ten times scores it once.

    `condid = 'IN'` is the intercept — a constant added to every score,
    and the value used for patients who match nothing.

    codecat: DX | PX | RX | DM (demographic: sex or age group) | IN
    """

    riskscore: str
    condid: str
    codecat: str
    code: str = ""
    weight: float = 0.0
    riskfrom: int = -365
    riskto: int = -1
    # Each end anchors independently, the same mechanism the inclusion
    # rules and covariate windows use (ms_computeriskscores.sas:107-117).
    # SAS defaults a blank to "indexdt" explicitly.
    riskfromanchor: str = "INDEXDT"
    risktoanchor: str = "INDEXDT"
    enctype: str = "**"
    pdx: str = "*"

    @property
    def is_intercept(self) -> bool:
        return self.condid.upper() == "IN" or self.codecat.upper() == "IN"


@dataclass(frozen=True)
class StratumLevel:
    """One output stratification level, from the USERSTRATA input file.

    `ms_processinputfiles.sas:1843` lowercases `levelvars`, converts `*`
    to a space (so `agegroup*sex` and `agegroup sex` are the same), and
    appends `agegroupnum` wherever `agegroup` appears so the output can
    be ordered numerically.

    An empty `levelvars` is the overall level — no stratification.
    """

    table_id: str            # t2cida, t2its, ...
    level_id: str            # the label written to the Level column
    levelvars: tuple[str, ...] = ()

    @classmethod
    def parse(cls, row: dict[str, Any]) -> "StratumLevel":
        raw = str(row.get("levelvars") or "").replace("*", " ").lower()
        cols = tuple(v for v in raw.split() if v)
        if "agegroup" in cols and "agegroupnum" not in cols:
            # SAS: tranwrd(levelvars, "agegroup", "agegroup agegroupnum")
            cols = tuple(
                x for c in cols
                for x in (("agegroup", "agegroupnum") if c == "agegroup"
                          else (c,))
            )
        return cls(
            table_id=str(row.get("tableid") or row.get("tableID") or "").lower(),
            level_id=str(row.get("levelid") or row.get("level") or "").strip(),
            levelvars=cols,
        )


@dataclass(frozen=True)
class StudyConfig:
    """Study-level parameters plus every cohort."""

    study_type: int
    start_date: date
    end_date: date
    cohorts: tuple[CohortConfig, ...]
    censor_date: date | None = None
    run_id: str = "qrp"
    covariates: tuple[Covariate, ...] = ()
    code_strength: tuple[tuple[str, float], ...] = ()
    inclusions: tuple[InclusionRule, ...] = ()
    strata: tuple[StratumLevel, ...] = ()
    risk_scores: tuple[RiskScoreCode, ...] = ()
    # ZIP lookup rows: (zip, statecode, hhs_region, cb_region, sdi).
    # Every field but the zip itself is optional — an incomplete lookup
    # is normal, and the Unknown rules in 47_geography.sql handle it.
    zipfile: tuple[
        tuple[str, str | None, str | None, str | None, float | None], ...
    ] = ()
    # UTILFILE rows: (cohortgrp, utiltype MED|DRUG, utilfrom, utilto)
    utilization: tuple[tuple[str, str, int, int], ...] = ()
    # NDC -> class lookup, for the distinct-class utilization count
    drug_classes: tuple[tuple[str, str], ...] = ()
    # (cohortgrp, code, labdatetype, op, lo, hi)
    lab_codes: tuple[tuple, ...] = ()
    # (cohortgrp, analysisnum, codecat, countmethod, topxx, from, to)
    mfu: tuple[tuple, ...] = ()

    def validate(self) -> None:
        if self.study_type != 2:
            raise NotImplementedError(
                f"this implementation covers Type 2 only (got type={self.study_type})"
            )
        if self.start_date > self.end_date:
            raise ValueError("start_date is after end_date")
        seen: set[str] = set()
        for c in self.cohorts:
            if c.cohortgrp in seen:
                raise ValueError(f"duplicate cohortgrp {c.cohortgrp!r}")
            seen.add(c.cohortgrp)
            c.validate()
        if not self.cohorts:
            raise ValueError("study defines no cohorts")
        if self.any_dose:
            known = {c for c, _ in self.code_strength}
            missing = {
                code
                for c in self.cohorts
                if c.needs_dose
                # exposure_codes is (code, codecat) pairs; dose applies
                # to dispensings, so only RX codes need a strength.
                for code, codecat, _ctype, _supply in c.exposure_codes
                if codecat == "RX" and code not in known
            }
            if missing:
                raise ValueError(
                    f"{len(missing)} exposure code(s) have a dose restriction "
                    f"but no strength in code_strength, e.g. "
                    f"{sorted(missing)[:5]}"
                )
        # SAS keys the enrollment build on ENROLLMENTNUM (`enr_&num.`),
        # while this implementation keys it on the parameters themselves
        # (coverage, gap, chart), which lets cohorts with identical
        # settings share one build.
        #
        # Those agree except in one case: if two cohorts declare the SAME
        # enrollmentnum but DIFFERENT parameters, SAS uses one build and
        # this would use two. That input file is self-contradictory, but
        # it would produce a silent parity difference rather than an
        # error, so say so.
        by_num: dict[int, tuple] = {}
        for c in self.cohorts:
            key = (c.coverage, c.enrol_gap, c.chart_required)
            prev = by_num.get(c.enrollment_num)
            if prev is not None and prev != key:
                errs_global = (
                    f"cohorts share enrollmentnum={c.enrollment_num} but "
                    f"declare different enrollment parameters "
                    f"{prev} vs {key}. SAS would build one enrollment set "
                    f"from the number; this builds one per parameter set, "
                    f"so results would differ. Fix the cohortfile."
                )
                raise ValueError(errs_global)
            by_num[c.enrollment_num] = key

        # SAS validates the inclusion file and WARNS rather than failing
        # (ms_processinputfiles.sas:645-705). Reproduced as warnings for
        # the same reason: a malformed file should not stop a run, but a
        # silently-different answer is worse than a noisy one.
        import warnings as _warnings

        # Grouped on the DERIVED condition key, matching SAS's
        # `by group conduse condlevel subcondlevel`. Using the raw
        # condlevel here mixed types once cond became an integer and
        # condlevel stayed a character value.
        by_sub: dict[tuple[str, str, int, int], dict[str, set]] = {}
        for r in self.inclusions:
            sub_key = (r.cohortgrp, r.criteria, r.cond, r.subcond)
            acc = by_sub.setdefault(sub_key, {"minrxdays": set(),
                                              "codedays": set()})
            acc["minrxdays"].add(r.minrxdays)
            acc["codedays"].add(r.codedays)
            if r.minrxdays > 1 and r.codecat != "RX":
                _warnings.warn(
                    f"minrxdays > 1 on a {r.codecat} code "
                    f"({r.cohortgrp} cond {r.cond}) — SAS only applies it "
                    f"to RX codes and resets it to 1",
                    stacklevel=2,
                )
        for sub_key, acc in by_sub.items():
            for field, values in acc.items():
                if len(values) > 1:
                    _warnings.warn(
                        f"different {field} values within one subcondlevel "
                        f"{sub_key}: {sorted(values)} — SAS expects "
                        f"exactly one",
                        stacklevel=2,
                    )

        seen_cov: set[int] = set()
        for cov in self.covariates:
            if cov.covarnum in seen_cov:
                raise ValueError(f"duplicate covarnum {cov.covarnum}")
            seen_cov.add(cov.covarnum)
            if cov.covfrom > cov.covto:
                raise ValueError(
                    f"covariate {cov.covarnum}: covfrom ({cov.covfrom}) "
                    f"is after covto ({cov.covto})"
                )
            # PX is a mainstream domain in real input files. CC (combo
            # covariates) is genuinely not implemented and must be
            # rejected rather than silently treated as DX.
            if cov.codecat == "CC":
                # a combo covariate must reference covariates that exist
                known_nums = {c.covarnum for c in self.covariates}
                absent = [n for n in cov.combo_refs if n not in known_nums]
                if absent:
                    raise ValueError(
                        f"combo covariate {cov.covarnum} references "
                        f"unknown covariate(s) {absent}"
                    )
                continue
            if cov.codecat not in {"DX", "RX", "PX"}:
                raise ValueError(
                    f"covariate {cov.covarnum}: codecat must be "
                    f"DX, RX or PX (got {cov.codecat!r}); CC (combo "
                    f"covariates) is not implemented"
                )

    # -- flags that gate whole stages, resolved once ------------------

    @property
    def any_dose_censoring(self) -> bool:
        """maxcumdose + cumdoseper censors the episode (ms_createpov4).

        SAS's own gate is `&maxcumdose. ne . and &cumdoseper. ne .`.
        Distinct from `any_dose`, which covers the index-date exclusions
        in ms_pov1dose.
        """
        return any(c.max_cum_dose is not None and c.cum_dose_per is not None
                   for c in self.cohorts)

    @property
    def any_dose(self) -> bool:
        return any(c.needs_dose for c in self.cohorts)

    @property
    def widest_lookback_days(self) -> int | None:
        """Largest number of days before an index date any rule can reach.

        `None` means UNBOUNDED — some rule looks back to the start of
        available history, so no lower bound may be applied to the scan.

        This replaces a hardcoded two-year cutoff. A study with a washout
        over 730 days, or any covariate with an unbounded lookback,
        silently lost claims: the filter is applied in the parquet
        reader, so the rows never enter the pipeline and nothing
        downstream can notice. Reported in review.
        """
        spans: list[int] = []
        for c in self.cohorts:
            # wash_per is int | None: a missing FupWashPer means "never
            # had an event", which is the strictest setting, not zero —
            # but for the SCAN bound it contributes nothing extra.
            spans.append(c.wash_per or 0)
            spans.append(c.fup_wash_per or 0)
            spans.append(c.cum_dose_per or 0)
        for cov in self.covariates:
            # Combo covariates are derived from other covariates and
            # scan no claims of their own, so they contribute no
            # lookback. Real files leave their covfrom blank, which
            # would otherwise read as unbounded and force a full-history
            # scan — 2.6x slower for no change in output.
            if cov.codecat == "CC":
                continue
            if cov.covfrom <= Covariate.UNBOUNDED_BEFORE:
                return None
            spans.append(-cov.covfrom)
        for incl in self.inclusions:
            spans.append(-incl.condfrom)
        for rs in self.risk_scores:
            spans.append(-rs.riskfrom)
        for _, _, util_from, _ in self.utilization:
            spans.append(-util_from)
        for row in self.mfu:
            spans.append(-int(row[5]))
        if self.lab_codes:
            spans.append(365)          # 76_labs.sql covariate window
        return max([s for s in spans if s is not None] or [0])

    @property
    def any_combo_covariates(self) -> bool:
        return any(c.codecat == "CC" for c in self.covariates)

    @property
    def any_covariates(self) -> bool:
        return bool(self.covariates)

    # USERSTRATA tableids this package produces. A study can request
    # others; SAS dispatches on this value (ms_cidanum.sas:2820-2831).
    IMPLEMENTED_TABLE_IDS = frozenset({"t2cida", "t2followuptime"})

    def cida_levels(self) -> tuple[StratumLevel, ...]:
        """USERSTRATA rows for the t2cida output table."""
        return tuple(s for s in self.strata if s.table_id == "t2cida")

    def followuptime_levels(self) -> tuple[StratumLevel, ...]:
        """USERSTRATA rows for the t2followuptime output table."""
        return tuple(s for s in self.strata
                     if s.table_id == "t2followuptime")

    @property
    def any_followuptime(self) -> bool:
        return bool(self.followuptime_levels())

    @property
    def unsupported_table_ids(self) -> tuple[str, ...]:
        """USERSTRATA tableids requested but not produced.

        Silently dropping one means the study asks for an output and
        receives nothing, with no error and no empty file to notice —
        the same failure shape as ignoring an inclusion rule. The one
        that prompted this was `t2followuptime`
        (`msoc.&RUNID._followuptime_cida`).
        """
        return tuple(sorted(
            {s.table_id for s in self.strata
             if s.table_id and s.table_id not in self.IMPLEMENTED_TABLE_IDS}
        ))

    @property
    def any_ioc(self) -> bool:
        return any(c.ioc_codes for c in self.cohorts)

    @property
    def any_mfu(self) -> bool:
        return bool(self.mfu)

    @property
    def any_labs(self) -> bool:
        return bool(self.lab_codes)

    @property
    def any_utilization(self) -> bool:
        return bool(self.utilization)

    @property
    def any_code_distribution(self) -> bool:
        """SAS skips this table when index is defined by labs, death or a
        date rather than by codes (ms_codedistribution.sas:7). Here the
        equivalent condition is simply whether any DEF codes exist."""
        return any(c.exposure_codes for c in self.cohorts)

    @property
    def any_geography(self) -> bool:
        return bool(self.zipfile)

    @property
    def any_risk_scores(self) -> bool:
        return bool(self.risk_scores)

    def risk_score_names(self) -> tuple[str, ...]:
        seen: list[str] = []
        for r in self.risk_scores:
            if r.riskscore not in seen:
                seen.append(r.riskscore)
        return tuple(seen)

    def denominator_cohorts(self) -> tuple[str, ...]:
        """Cohorts whose denominator SAS would actually compute.

        Two gates, both from SAS:

        * `OUTPUTDENOM = N` on the cohort — no denominator at all.
        * **`minrxdays > 1` anywhere in the inclusion criteria forces
          it off**, with a warning: "Outputdenom set to N for <group>
          because minrxdays is used in inclusion/exclusion criteria"
          (ms_setnumloopmacrovars.sas:898-900). A pro-rated supply
          requirement makes the eligible-member count incoherent, so
          SAS refuses to emit one rather than emit a wrong one.

        Ignoring either produced a denominator for a study that would
        get none from SAS — a plausible number with no counterpart.
        """
        forced_off = any(r.minrxdays > 1 for r in self.inclusions)
        out = []
        for c in self.cohorts:
            if c.output_denom == "N":
                continue
            if forced_off and self.study_type <= 2:
                continue
            out.append(c.cohortgrp)
        return tuple(out)

    @property
    def denominators_suppressed_by_minrxdays(self) -> bool:
        return (self.study_type <= 2
                and any(r.minrxdays > 1 for r in self.inclusions)
                and any(c.output_denom != "N" for c in self.cohorts))

    @property
    def any_cida_tables(self) -> bool:
        return bool(self.cida_levels())

    @property
    def any_inclusion_dose(self) -> bool:
        """Any inclusion rule carrying a dose threshold."""
        return any(r.mincumdose is not None or r.minafdd is not None
                   or r.maxafdd is not None for r in self.inclusions)

    @property
    def any_inclusions(self) -> bool:
        return bool(self.inclusions)

    @property
    def unsupported_inclusions(self) -> tuple[str, ...]:
        """Inclusion features present in the study but not implemented."""
        out: list[str] = []
        # INDEXDT and EPISODEENDDT are both applied.
        #
        # INDEXDT_EXP anchors on `indexdt_exp`: the date exposure
        # actually BEGAN inside a pregnancy's exposure window, clamped
        # to the start of that window (ms_createmicohorts.sas:764 —
        # the comparator/Type 4 macro, which is the right source HERE
        # precisely because this is Type 4 logic). It
        # is distinct from `indexdt`, which for a pregnancy cohort is
        # usually the pregnancy start date. Every use in the macros is
        # gated on `type = 4`, so it is not reachable from Type 2 — but
        # a study file can still carry the value, so it warns rather
        # than being silently treated as an index-date anchor.
        anchors = {r.condfromanchor for r in self.inclusions} | {
            r.condtoanchor for r in self.inclusions}
        unknown = anchors - {"INDEXDT", "EPISODEENDDT"}
        if unknown:
            out.append(
                f"condition anchors {sorted(unknown)} — not applied, so "
                f"those windows fall back to the index date"
            )
        cov_anchors = {c.covfromanchor for c in self.covariates} | {
            c.covtoanchor for c in self.covariates}
        cov_unknown = cov_anchors - {"INDEXDT", "EPISODEENDDT"}
        if cov_unknown:
            out.append(
                f"covariate anchors {sorted(cov_unknown)} — not applied, "
                f"so those windows fall back to the index date"
            )
        risk_anchors = {r.riskfromanchor for r in self.risk_scores} | {
            r.risktoanchor for r in self.risk_scores}
        risk_unknown = risk_anchors - {"INDEXDT", "EPISODEENDDT"}
        if risk_unknown:
            out.append(
                f"risk-score anchors {sorted(risk_unknown)} — not "
                f"applied, so those windows fall back to the index date"
            )
        return tuple(out)

    @property
    def effective_censor_date(self) -> date:
        return self.censor_date or self.end_date


# --------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------


def _as_date(v: Any) -> date | None:
    """SAS date integer, ISO string, or date. See qrp.inputfile.sas_date."""
    from .inputfile import sas_date

    return sas_date(v)


@overload
def _int(v: Any, default: int) -> int: ...


@overload
def _int(v: Any, default: None) -> int | None: ...


def _int(v: Any, default: int | None = 0) -> int | None:
    """Parse an integer, falling back to `default` when absent.

    Overloaded so the return type follows the default: passing an int
    default yields an int, passing None yields `int | None`. Without
    this the single signature is `int | None`, and ~20 call sites that
    genuinely cannot produce None have to be either cast or ignored.
    """
    if v is None or v == "":
        return default
    return int(v)


def _float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def _bool_yn(v: Any, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().upper() in {"Y", "YES", "TRUE", "1"}


# Scalar parameters SAS branches on that this package does not read.
# Ignoring one silently gives a plausible answer computed under
# different rules — the failure mode this package has been most prone
# to — so the presence of any of them is reported rather than dropped.
#
# Found by grepping the macros for `%if "&param" = "Y"` and checking
# each against what config.py reads. `outputdenom` was in this set
# until it was implemented.
UNREAD_SCALARS: dict[str, str] = {
    "othersex": "forces sex='O' into the output shell even when no "
                "patient has it (ms_cidacov.sas:114)",
    "includelinkedonly": "restricts the cohort to linked members",
    "calculate_adherence": "adds adherence metrics",
    "datadrivenqueryperiod": "derives the query period from the data "
                             "rather than the study dates",
    "agegroup_out": "controls whether age groups appear in the output",
    "geog_out": "controls whether geography appears in the output",
    "psmatch": "propensity-score matching (comparator designs)",
    "psstratification": "propensity-score stratification",
    "psiptw": "inverse-probability weighting",
}


def _warn_unread_scalars(params: dict) -> None:
    """Report scalar parameters the study sets and this package ignores.

    A parameter set to "N" is not a problem: not doing something this
    package already does not do is agreement, not divergence. Only an
    active value is reported.
    """
    import warnings

    active = [
        f"{k} ({UNREAD_SCALARS[k]})"
        for k, v in params.items()
        if k in UNREAD_SCALARS
        and str(v).strip().upper() not in ("", "N", "NO", "0", "NONE")
    ]
    if active:
        warnings.warn(
            "the study sets parameter(s) this implementation does not "
            "read, so results are computed as if they were off: "
            + "; ".join(sorted(active)),
            stacklevel=3,
        )


def _warn_denominator_suppressed(study: StudyConfig) -> None:
    """SAS warns when minrxdays forces the denominator off; so do we.

    Silently omitting a deliverable the study asked for is the failure
    mode this whole package has been most prone to.
    """
    import warnings

    if study.denominators_suppressed_by_minrxdays:
        warnings.warn(
            "denominators will NOT be computed because an inclusion rule "
            "uses minrxdays > 1 — SAS disables OUTPUTDENOM in that case "
            "(ms_setnumloopmacrovars.sas:898), since a pro-rated supply "
            "requirement makes the eligible-member count incoherent",
            stacklevel=3,
        )
    off = [c.cohortgrp for c in study.cohorts if c.output_denom == "N"]
    if off:
        warnings.warn(
            f"cohort(s) {sorted(off)} set OUTPUTDENOM=N, so no "
            f"denominator is computed for them",
            stacklevel=3,
        )


def _warn_unsupported_tables(study: StudyConfig) -> None:
    """A requested output table this package does not produce is ABSENT,
    not empty — a downstream step expecting it finds nothing at all.

    Lives here rather than in load_study() so BOTH entry points warn:
    studies built from a dict were previously silent.
    """
    import warnings

    if study.unsupported_table_ids:
        warnings.warn(
            "userstrata requests output table(s) this implementation does "
            "not produce: " + ", ".join(study.unsupported_table_ids)
            + " — those tables will be ABSENT from the output, not empty, "
              "so a downstream step expecting them will find nothing",
            stacklevel=3,
        )


def load_study(path: str | Path, lookup: str | Path | None = None) -> StudyConfig:
    """Load and validate a study definition from a QRP input file.

    Handles both shapes:
      * real `create_json.sas` output, where top-level keys are SAS
        DATASET names and `QRP_PARAMETERS` maps logical -> actual, and
      * hand-written files using literal keys (the demo studies).

    See `qrp.inputfile` for the indirection; `qrp inspect --study <f>`
    prints what resolved before you commit to a run.
    """
    from . import inputfile as _inputfile

    inp = _inputfile.load(path, lookup)
    if inp.unresolved:
        import warnings
        warnings.warn(
            "input file names tables that are absent from the JSON: "
            + "; ".join(inp.unresolved),
            stacklevel=2,
        )
    present_unimpl = [
        t for t in _inputfile.UNIMPLEMENTED_TABLES if inp.tables.get(t)
    ]
    if present_unimpl:
        import warnings
        warnings.warn(
            f"study supplies {', '.join(present_unimpl)}, which this "
            f"implementation does not yet apply — results will be broader "
            f"than the SAS run",
            stacklevel=2,
        )
    study = load_study_dict(
        {**inp.tables, "qrp_parameters_scalars": inp.scalars}
    )
    # Inclusion rules are applied, but not every variant of them. Warn
    # about the ones parsed and ignored, since those make the cohort
    # broader than SAS's — the dangerous direction.
    import warnings

    if study.unsupported_inclusions:
        warnings.warn(
            "inclusioncodes uses features this implementation does not "
            "apply: " + ", ".join(study.unsupported_inclusions)
            + " — those rules are ignored, so the cohort will be broader "
              "than the SAS run",
            stacklevel=2,
        )
    return study


def _parse_inclusions(
    incl_rows: list[dict[str, Any]],
) -> list["InclusionRule"]:
    """Build the inclusion rules from INCLUSIONCODES.

    The most intricate of the parsers: three levels of nesting derived
    from character columns, per-rule code lists, and the cond/subcond
    keys that a review found were being collapsed.
    """
    _incl_rows = incl_rows
    _cond_no: dict[tuple, int] = {}
    _sub_no: dict[tuple, int] = {}
    inclusions_list: list[InclusionRule] = []
    for r in _incl_rows:
        grp = str(r.get("group") or r.get("cohortgrp") or "")
        # Real input files come in two shapes. Some carry an
        # `indexcriteria` column saying INC/EXC outright; others carry
        # `condinclusion`, where 0 means EXCLUDE and 1 means INCLUDE
        # (SAS reads CondInclusion directly — ms_cidadenom.sas:159).
        #
        # Defaulting to "INC" when neither is present turned every
        # EXCLUSION rule into an inclusion REQUIREMENT. On the real
        # 40-cohort study that meant patients had to HAVE the
        # splenectomy codes they were supposed to be excluded for:
        # 42,708 episodes became 58.
        crit = str(r.get("indexcriteria") or "").upper()
        if not crit:
            ci = r.get("condinclusion")
            if ci is not None and str(ci).strip() != "":
                crit = "INC" if _int(ci, 1) else "EXC"
            else:
                crit = "INC"
        clvl = str(r.get("condlevel") or "1").upper()
        slvl = str(r.get("subcondlevel") or "1").upper()

        ckey = (grp, crit, clvl)
        if ckey not in _cond_no:
            _cond_no[ckey] = 1 + len({k for k in _cond_no if k[:2] == (grp, crit)})
        cond = _cond_no[ckey]

        skey = (grp, crit, clvl, slvl)
        if skey not in _sub_no:
            _sub_no[skey] = 1 + len({k for k in _sub_no if k[:3] == ckey})
        subcond = _sub_no[skey]

        inclusions_list.append(InclusionRule(
            cohortgrp=grp,
            cond=cond,
            subcond=subcond,
            condlevel=_int(r.get("condlevel"), 1) if str(
                r.get("condlevel") or "").isdigit() else cond,
            criteria=crit,
            codecat=str(r.get("codecat") or "DX").upper(),
            condfrom=_int(r.get("condfrom"), -365),
            condto=_int(r.get("condto"), -1),
            condfromanchor=(str(r.get("condfromanchor") or "").strip().upper()
                            or "INDEXDT"),
            condtoanchor=(str(r.get("condtoanchor") or "").strip().upper()
                          or "INDEXDT"),
            codedays=max(1, _int(r.get("codedays"), 1) or 1),
            minrxdays=max(1, _int(r.get("minrxdays"), 1) or 1),
            mincumdose=_float(r.get("mincumdose")),
            minafdd=_float(r.get("minafdd")),
            maxafdd=_float(r.get("maxafdd")),
            subcondlevel=slvl,
            # `or "1"` would be wrong here: an integer 0 is falsy, so
            # `0 or "1"` yields "1" and a sub-EXCLUSION silently becomes
            # a sub-inclusion. Check for absence explicitly.
            subcond_inclusion=(
                str(r["subcondinclusion"]).strip().upper()
                not in ("0", "N", "NO", "FALSE")
                if r.get("subcondinclusion") is not None
                and str(r.get("subcondinclusion")).strip() != ""
                else True
            ),
            codes=tuple(str(c) for c in (r.get("codes") or []))
                  or ((str(r["code"]),) if r.get("code") else ()),
        ))

    return inclusions_list


def _parse_cohort_codes(
    code_rows: list[dict[str, Any]],
) -> tuple[dict, dict, dict]:
    """Split COHORTCODES into (codes, stockgroups, care settings) by group.

    Extracted from `load_study_dict`, which was 356 lines of one block
    per input table. Each block is independently readable; together they
    were not.

    Returns three maps keyed by cohort group:
      codes_by_group  role -> [(code, codecat, codetype, code_supply)]
      stock_by_group  code -> stockgroup
      care_by_group   [(code, enctype, pdx)] for EVENT codes
    """
    codes_by_group: dict[str, dict[str, list[tuple[str, str, str, int | None]]]] = {}
    stock_by_group: dict[str, dict[str, str]] = {}
    care_by_group: dict[str, list[tuple[str, str, str]]] = {}

    for r in code_rows:
        g = r.get("group") or r.get("cohortgrp")
        if not g:
            continue
        # (code, codecat) — NOT code alone. A real study defines
        # exposure across RX, PX and DX simultaneously (960/150/14 in the
        # file seen), and two of its cohorts are defined purely by HCPCS
        # procedure codes. Dropping codecat made those cohorts extract
        # from dispensing only, so they came out EMPTY. Reported in
        # review.
        bucket = codes_by_group.setdefault(
            str(g), {"DEF": [], "EVENT": [], "IOC": [], "TRUNK": []})
        codecat = str(r.get("codecat") or "RX").upper()
        crit = str(r.get("indexcriteria") or "").upper()
        fup = str(r.get("fupcriteria") or "").upper()
        # fupcriteria='IOC' marks a washout-only code: it never defines
        # an index or an outcome, only disqualifies an episode whose
        # washout window contains it.
        if fup == "IOC":
            key = "IOC"
        elif fup == "DEF" or crit == "EVENT":
            # The OUTCOME. SAS routes on FUPCRITERIA, not indexcriteria:
            # `if fupcriteria in('DEF') then output _FUPEvent`
            # (ms_cidanum.sas:1684 — the TYPE 2 path;
            # ms_createmicohorts.sas is the comparator/Type 4 macro and
            # is not authoritative here).
            #
            # `indexcriteria = 'EVENT'` is not a value SAS writes, but
            # it is unambiguous and some hand-built study files use it,
            # so it is honoured too.
            key = "EVENT"
        elif crit == "DEF":
            key = "DEF"
        elif crit == "FUT":
            # TRUNCATION codes. A FUT claim inside an episode ENDS it:
            #
            #   if fut and trunkdt and trunkdt <= EpisodeEndDt
            #       then EpisodeEndDt = trunkdt;
            #   (ms_createptsmasterlist.sas:152)
            #
            # where trunkdt is the earliest FUT claim overlapping
            # [EpisodeStartDt, EpisodeEndDt] (ms_createpov4.sas:155-167,
            # commented "Truncate (potentially extended using Episode
            # Extension) episodes with FUT").
            #
            # Skipping them left every affected episode too long: on the
            # study compared, 3,077 of 31,440 episodes ran past SAS's
            # end date and NOT ONE was shorter.
            key = "TRUNK"
        else:
            # INDEXCRITERIA = 'NOT' with no follow-up role.
            # FUT goes to SAS's washout-for-truncation set
            # (`_GroupWashForTrunk`, ms_cidanum.sas:1766), which this
            # package does not model.
            #
            # These used to fall through to EVENT, and they dominate a
            # real file: one cohort of the study compared has 54 DEF
            # codes, ONE outcome code, and 4,957 FUT codes. Treating
            # FUT as outcomes gave 4,958 event codes instead of 1, and
            # 11,196 events against SAS's 3 — which then dropped 58
            # episodes through the blackout-event rule.
            continue
        if r.get("code"):
            # CODESUPPLY overrides the claim's own RxSup
            # (SAS's CODESUPPLY handling (exact line unverified) — `if not missing(codesupply)
            # then RxSup = CodeSupply`). It is per CODE, not per cohort:
            # 150 of 1,124 rows carry it in the real study file, all of
            # them PX, because a procedure claim has no days-supply.
            bucket[key].append((
                str(r["code"]), codecat,
                # "" when the file does not say; the SQL treats an
                # empty codetype as "match any", so a file without the
                # column behaves as it did before.
                str(r.get("codetype") or "").strip().upper(),
                _int(r.get("codesupply"), 0) or None))
            if key in ("DEF", "TRUNK"):
                # TRUNK codes carry a stockgroup and stockpile within
                # it, exactly as exposure codes do. Capturing it only
                # for DEF left every truncation code in `_default`, so
                # unrelated drugs chained together and pushed dates far
                # past where SAS puts them.
                stock_by_group.setdefault(str(g), {})[str(r["code"])] = (
                    str(r.get("stockgroup") or "").strip() or "_default"
                )
            else:
                for enctype, pdx in parse_care_setting(
                    r.get("caresettingprincipal")
                ):
                    care_by_group.setdefault(str(g), []).append(
                        (str(r["code"]), enctype, pdx)
                    )


    return codes_by_group, stock_by_group, care_by_group


def _query_period(params: dict[str, Any],
                  monitoring: list[dict[str, Any]]) -> tuple[date, date, date | None]:
    """(start, end, censor) for the query period.

    A study states its period in ONE of two places, and both occur in
    real input files:

      * `startdate` / `enddate` scalars in QRP_PARAMETERS, or
      * the MONITORING file — `startdate`, `indenddate`, `fupenddate`

    Only the scalars were read, and a study using the monitoring file
    silently fell back to a hardcoded 2010-2015. On the real 40-cohort
    study seen, whose period is 2016-2025, that meant the query period
    did not overlap the data at all: SAS found 31,464 episodes and this
    package found 6. A wrong answer, with no error.

    MONITORING's end date follows ms_processinputfiles.sas:996-1010:
    `indenddate` when given, otherwise FUPDRIVEN takes `fupenddate`.
    """
    start = _as_date(params.get("startdate"))
    end = _as_date(params.get("enddate"))
    censor = _as_date(params.get("censordate"))

    if monitoring and not (start and end):
        row = monitoring[0]
        start = start or _as_date(row.get("startdate"))
        fup = _as_date(row.get("fupenddate"))
        end = end or _as_date(row.get("indenddate")) or fup
        censor = censor or fup

    if not start or not end:
        raise ValueError(
            "the study does not state a query period. Give startdate and "
            "enddate in QRP_PARAMETERS, or a monitoring file with "
            "startdate and indenddate/fupenddate.\n  This used to fall "
            "back to 2010-2015, which silently produced a cohort from "
            "the wrong years rather than an error."
        )
    return start, end, censor


def load_study_dict(raw: dict[str, Any]) -> StudyConfig:
    def rows(name: str) -> list[dict[str, Any]]:
        return [
            {str(k).lower(): v for k, v in r.items()}
            for r in (raw.get(name) or raw.get(name.upper()) or [])
        ]

    # Scalars arrive either pre-resolved from qrp.inputfile, or as a
    # single-row dict in a hand-written demo file.
    params: dict[str, Any] = dict(raw.get("qrp_parameters_scalars") or {})
    if not params and raw.get("qrp_parameters"):
        first = (raw["qrp_parameters"] or [{}])[0]
        params = {str(k).lower(): v for k, v in first.items()}
    params.update({str(k).lower(): v for k, v in (raw.get("study") or {}).items()})

    cohortfile = {r["cohortgrp"]: r for r in rows("cohortfile")}
    type2file = {r.get("group", r.get("cohortgrp")): r for r in rows("type2file")}

    (codes_by_group, stock_by_group,
     care_by_group) = _parse_cohort_codes(rows("cohortcodes"))
    cohorts: list[CohortConfig] = []
    for grp, cf in cohortfile.items():
        t2 = type2file.get(grp, {})
        codes = codes_by_group.get(grp, {})
        supply_rows = [
            _int(r.get("codesupply"), None)
            for r in rows("cohortcodes")
            if (r.get("group") or r.get("cohortgrp")) == grp
            and str(r.get("indexcriteria") or "").upper() == "DEF"
            and r.get("codesupply") not in (None, "")
        ]
        cohorts.append(
            CohortConfig(
                cohortgrp=str(grp),
                enrollment_num=_int(cf.get("enrollmentnum"), 1),
                coverage=str(cf.get("coverage") or "MD").upper(),
                enrol_gap=_int(cf.get("enrolgap"), 0),
                chart_required=_bool_yn(cf.get("chartres")),
                enr_days=_int(cf.get("enrdays"), 183),
                sex=_codes(cf.get("sex"), ("F", "M", "U", "A")),
                race=_codes(cf.get("race"), ("0", "1", "2", "3", "4", "5", "M")),
                hispanic=_codes(cf.get("hispanic"), ("Y", "N", "U")),
                age_strata=AgeStrata.parse(cf.get("agestrat")),
                wash_per=_int(t2.get("t2washper"), None),
                point=_bool_yn(t2.get("point")),
                episode_gap=_int(t2.get("episodegap"), 0),
                episode_gap_type=str(t2.get("episodegaptype") or "F").upper()[:1] or "F",
                exp_ext_per=_int(t2.get("expextper"), 0),
                min_epis_dur=_int(t2.get("minepisdur"), 1) or 1,
                max_epis_dur=_int(t2.get("maxepisdur"), 0),
                min_days_supp=_int(t2.get("mindaysupp"), 0),
                at_risk_start=_int(t2.get("t2atriskstart"), 0),
                blackout_per=_int(t2.get("blackoutper"), 0),
                # absent -> None ("never had an event"), per SAS.
                fup_wash_per=_int(t2.get("t2fupwashper"), None),
                event_count=_int(t2.get("eventcount"), 0),
                req_days_aft_ind=_int(cf.get("reqdaysaftind"), 0),
                req_days_aft_epi=_int(t2.get("reqdaysaftepi"), 0),
                censor_death=_bool_yn(t2.get("censor_dth"), default=True),
                output_denom=str(
                    t2.get("outputdenom") or "Y").strip().upper()[:1] or "Y",
                min_cum_dose=_float(t2.get("mincumdose")),
                max_cum_dose=_float(t2.get("maxcumdose")),
                cum_dose_per=_int(t2.get("t2cumdoseper"), None),
                min_cfdd=_float(t2.get("mincfdd")),
                max_cfdd=_float(t2.get("maxcfdd")),
                # Kept for the signature/description only; the value
                # that matters is carried per code on exposure_codes.
                code_supply=next((v for v in supply_rows if v is not None),
                                 None),
                exposure_codes=tuple(codes.get("DEF", ())),
                exposure_stockgroups=tuple(
                    sorted(stock_by_group.get(str(grp), {}).items())
                ),
                event_care_settings=tuple(care_by_group.get(str(grp), ())),
                event_codes=tuple(codes.get("EVENT", ())),
                ioc_codes=tuple(codes.get("IOC", ())),
                trunc_codes=tuple(codes.get("TRUNK", ())),
            )
        )

    _period = _query_period(params, rows("monitoringfile"))

    strata = tuple(StratumLevel.parse(r) for r in rows("userstrata"))

    mfu = tuple(
        (
            str(r.get("group") or r.get("cohortgrp") or ""),
            _int(r.get("analysisnum"), 1),
            str(r.get("codecat") or "DX").upper(),
            # codecount ranks by claims, patcount by distinct patients.
            # They give different orderings; SAS defaults to codecount.
            str(r.get("countmethod") or "CODECOUNT").upper(),
            _int(r.get("topxx"), 20),
            _int(r.get("mfufrom"), -365),
            _int(r.get("mfuto"), -1),
        )
        for r in rows("mfufile")
        if (r.get("group") or r.get("cohortgrp"))
    )

    lab_codes = tuple(
        (
            str(r.get("group") or r.get("cohortgrp") or ""),
            str(r.get("code") or ""),
            (str(r.get("labdatetype") or "LRO").upper() + "   ")[:3],
            # codetype dispatches the extraction path
            # (ms_extractlabs.sas:201, 301):
            #   substr(codetype,1,2) = '01' -> lookup, '02' -> LOINC,
            #                          other -> PX
            #   substr(codetype,3,1) = the RESULT TYPE (N numeric,
            #                          C character)
            (str(r.get("codetype") or "01N").upper() + "   ")[:2],
            (str(r.get("codetype") or "01N").upper() + "   ")[2:3] or "N",
            # LAB01 combination, upcased and trimmed as SAS does
            *(str(r.get(f) or "").strip().upper() for f in (
                "ms_test_name", "ms_test_sub_category", "specimen_source",
                "ms_result_unit", "result_type", "fast_ind", "pt_loc")),
            *parse_lab_result(r.get("labresult")),
        )
        # SAS names this LABCODESMAP; some studies use LABCODES.
        for r in (rows("labcodes") or rows("labcodesmap"))
        if r.get("code")
    )

    utilization = tuple(
        (
            str(r.get("group") or r.get("cohortgrp") or ""),
            str(r.get("utiltype") or r.get("type") or "MED").upper(),
            _int(r.get("utilfrom"), -365),
            _int(r.get("utilto"), -1),
        )
        for r in rows("utilfile")
        if (r.get("group") or r.get("cohortgrp"))
    )

    drug_classes = tuple(
        (str(r["code"]), str(r.get("classname") or r.get("class") or ""))
        for r in rows("drugclassfile")
        if r.get("code")
    )

    zipfile = tuple(
        (
            str(r.get("zip") or "").strip(),
            str(r.get("statecode") or "").strip() or None,
            str(r.get("hhs_region") or r.get("hhs_reg") or "").strip() or None,
            str(r.get("cb_region") or r.get("cb_reg") or "").strip() or None,
            _float(r.get("sdi")),
        )
        for r in rows("zipfile")
        if str(r.get("zip") or "").strip()
    )

    risk_scores = tuple(
        RiskScoreCode(
            riskscore=str(r.get("riskscore") or "").strip().upper(),
            condid=str(r.get("condid") or "").strip().upper(),
            codecat=str(r.get("codecat") or "DX").strip().upper(),
            code=str(r.get("code") or "").strip(),
            weight=_float(r.get("weight")) or 0.0,
            riskfrom=_int(r.get("riskfrom"), -365),
            riskto=_int(r.get("riskto"), -1),
            riskfromanchor=(str(r.get("riskfromanchor") or "").strip().upper()
                            or "INDEXDT"),
            risktoanchor=(str(r.get("risktoanchor") or "").strip().upper()
                          or "INDEXDT"),
            # riskscorecodes carries its own care-setting restriction
            **(lambda pairs: {"enctype": pairs[0][0], "pdx": pairs[0][1]})(
                parse_care_setting(r.get("caresettingprincipal"))
            ),
        )
        for r in rows("riskscorecodes")
        if str(r.get("riskscore") or "").strip()
    )

    # Derive `cond` and `subcond` the way SAS does
    # (ms_processinputfiles.sas:715-740): renumber the CHARACTER
    # condlevel/subcondlevel columns, per cohort and criteria, in file
    # order. Reading a numeric `cond` column straight from the input is
    # wrong — it does not exist there, so every rule would land in
    # condition 1 and be ORed instead of ANDed.
    inclusions_list = _parse_inclusions(rows("inclusioncodes"))
    inclusions = tuple(inclusions_list)

    # COVARIATECODES carries ONE ROW PER CODE with covarnum repeated —
    # verified against a real input file (4,385 rows, 49 covariates, up
    # to 1,432 codes for one of them). The earlier parser expected one
    # row per covariate with a `codes` list, which is a shape my own
    # fixtures invented; on a real file every row became a separate
    # covariate and validation rejected the duplicate covarnums.
    #
    # The window and anchor attributes are taken from the first row of
    # each covarnum: SAS validates that they are constant within a
    # covariate (ms_processinputfiles.sas), so any row will do.
    _cov_rows: dict[int, dict[str, Any]] = {}
    _cov_codes: dict[int, list[str]] = {}
    for r in rows("covariatecodes"):
        num = _int(r.get("covarnum"), 0)
        _cov_rows.setdefault(num, r)
        code = str(r.get("code") or "").strip()
        if code:
            _cov_codes.setdefault(num, []).append(code)

    covariates = tuple(
        Covariate(
            covarnum=num,
            # Real files have no `covarname`; SAS derives a label from
            # the code grouping. `stockgroup` carries it in practice.
            covarname=str(
                r.get("covarname") or r.get("stockgroup") or f"covar{num}"
            ),
            codecat=str(r.get("codecat") or "DX").upper(),
            covfrom=_int(r.get("covfrom"), Covariate.UNBOUNDED_BEFORE),
            covto=_int(r.get("covto"), Covariate.UNBOUNDED_AFTER),
            covfromanchor=(str(r.get("covfromanchor") or "").strip().upper()
                           or "INDEXDT"),
            covtoanchor=(str(r.get("covtoanchor") or "").strip().upper()
                         or "INDEXDT"),
            dateonly=_bool_yn(r.get("dateonly")),
            # A supplied `codes` list still works, for hand-written
            # studies and the test fixtures.
            codes=tuple(str(c) for c in (r.get("codes") or []))
                  or tuple(_cov_codes.get(num, ())),
            combo_sql=(parse_combo(r.get("code"))[0]
                       if str(r.get("codecat") or "").upper() == "CC"
                       else ""),
            combo_refs=(parse_combo(r.get("code"))[1]
                        if str(r.get("codecat") or "").upper() == "CC"
                        else ()),
        )
        for num, r in sorted(_cov_rows.items())
    )

    code_strength = tuple(
        (str(r["code"]), float(r["strength"]))
        for r in rows("codestrength")
        if r.get("code") and r.get("strength") not in (None, "")
    )

    study = StudyConfig(
        study_type=_int(params.get("type"), 2),
        covariates=covariates,
        inclusions=inclusions,
        strata=strata,
        risk_scores=risk_scores,
        zipfile=zipfile,
        utilization=utilization,
        drug_classes=drug_classes,
        lab_codes=lab_codes,
        mfu=mfu,
        code_strength=code_strength,
        start_date=_period[0],
        end_date=_period[1],
        censor_date=_period[2],
        run_id=safe_run_id(params.get("runid")),
        cohorts=tuple(cohorts),
    )
    study.validate()
    _warn_unsupported_tables(study)
    _warn_denominator_suppressed(study)
    _warn_unread_scalars(params)
    return study


__all__ = [
    "AgeStrata",
    "AgeStratum",
    "CohortConfig",
    "Covariate",
    "StudyConfig",
    "load_study",
    "load_study_dict",
    "replace",
]
