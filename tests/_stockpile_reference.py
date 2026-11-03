"""Reference transliteration of SAS's ms_stockpiling.sas, for one patient and
and stockgroup. Used to show the sample, and as the test oracle."""
import datetime as dt, re

def in_ranges(v, spec):
    spec = (spec or "0<-HIGH").upper().replace("LOW", "0<")
    for part in spec.split():
        m = re.fullmatch(r"([\d.]+)(<?)-(<?)([\d.]+|HIGH)", part)
        lo, lo_open, hi_open, hi = float(m[1]), m[2] == "<", m[3] == "<", m[4]
        hi = float("inf") if hi == "HIGH" else float(hi)
        if (v > lo if lo_open else v >= lo) and (v < hi if hi_open else v <= hi):
            return True
    return False

OPS = {"a": sum, "n": min, "x": max, "m": lambda xs: sum(xs) / len(xs)}

def stockpile(fills, sameday="aa", suprange=None, amtrange=None, percentdays=None):
    """fills: [(date, rxsup, rxamt)] -> (records, excluded)"""
    kept = [f for f in fills if in_ranges(f[1], suprange) and in_ranges(f[2], amtrange)]
    excluded = [f for f in fills if f not in kept]
    so, ao = (sameday + "aa")[0].lower(), (sameday + "aa")[1].lower()
    so, ao = (so if so in OPS else "a"), (ao if ao in OPS else "a")
    days = {}
    for d, s, a in sorted(kept):
        days.setdefault(d, []).append((s, a))
    recs, lexp, lsup, ep = [], None, None, 1
    for i, d in enumerate(sorted(days)):
        sups, amts = [s for s, _ in days[d]], [a for _, a in days[d]]
        sup, amt, flag, date = OPS[so](sups), OPS[ao](amts), 0, d
        if i > 0:
            overlap = (lexp - d).days + 1
            if overlap > 0:
                if percentdays is None or overlap / lsup <= percentdays:
                    date = lexp + dt.timedelta(days=1)
                else:
                    ep, flag = ep + 1, 1
            else:
                ep += 1
        exp = date + dt.timedelta(days=sup - 1)
        recs.append(dict(orig=d, date=date, exp=exp, sup=sup, amt=amt, ep=ep, flag=flag, n=len(sups)))
        lexp, lsup = exp, sup
    if percentdays is not None:                      # truncate before a flagged new sequence
        for e in {r["ep"] for r in recs}:
            nxt = [r for r in recs if r["ep"] == e + 1]
            if nxt and max(r["flag"] for r in nxt):
                start = min(r["date"] for r in nxt)
                for r in recs:
                    if r["ep"] == e and r["exp"] >= start:
                        new_sup = (start - r["date"]).days
                        r["amt"] *= new_sup / r["sup"]; r["sup"] = new_sup
                        r["exp"] = start - dt.timedelta(days=1); r["cut"] = True
    return recs, excluded
