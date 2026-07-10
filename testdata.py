"""
================================================================================
 LGD ENGINE — TESTCASE-WISE TEST DATA GENERATOR  (SRF-GROUP EDITION)
================================================================================
 Selection unit = SingleNameSRF GROUP (a whole exposure), not a single row.

 For each of the 16 batch test cases:
   1. Group the consolidated sheet by SingleNameSRF.
   2. Evaluate SRFs in ASCENDING SingleNameSRF order.
   3. An SRF QUALIFIES when:
        - securedness matches at the SRF level (collateral count across the
          whole exposure), AND
        - guarantee presence matches at the SRF level, AND
        - at least ONE row in the SRF has the required BorrowerBRR-vs-GBRR
          relationship (relationship judged per-row).
   4. Take UP TO 2 qualifying SRFs per test case.
   5. Output ALL rows of those SRFs, with ALL 97 original columns preserved,
      plus a few audit columns.

 BRR/GBRR handling
   - Natural SRFs (a real row already satisfies the relationship) are preferred
     and are copied UNCHANGED — the ideal, highest-fidelity test data.
   - If fewer than 2 natural SRFs exist, the script falls back:
       BORROW : on a structurally-correct SRF, adjust GBRR on the matching row(s)
                to force the relationship (Option A — rating cell only).
       FORCE  : as a last resort, write clean BRR/GBRR on one row of the SRF.
     Any change is flagged per row and the original value preserved.

 Missing-value cases (14/15/16) null/blank BRR or GBRR on the SRF's rows.

 Output: one workbook, one sheet per test case + a _SUMMARY sheet.

 RUN:
   python lgd_testdata_generator_srf.py \
       --input cms_consolidated_report.xlsx \
       --output testcase_testdata.xlsx \
       --srf-per-case 2
================================================================================
"""

import argparse
import logging
import sys
import re

import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("lgd_testdata_srf")


# ==============================================================================
# 1. CONFIG
# ==============================================================================
class Config:
    # --- Grouping key: the whole-exposure identifier ---
    COL_SRF = "SingleNameSRF"

    # --- Rating columns (confirmed from the query) ---
    COL_BRR  = "BorrowerBRR"
    COL_GBRR = "GBRR"

    # --- Structural columns ---
    COL_COLLATERAL    = "CollateralID"
    COL_FACILITY      = "FacilityID"
    COL_GUARANTEE_ID  = "GuaranteeID"
    COL_GUAR_TYPE     = "GuarantorType"

    # --- How many SRF groups to emit per test case ---
    SRF_PER_CASE = 2

    # --- Borrowing approach: "A" = swap rating cell only (recommended) ---
    BORROW_MODE = "A"

    # --- Input sheet (name or 0-based index) ---
    INPUT_SHEET = 0

    # --- Securedness thresholds (collateral count across the SRF) ---
    # 0 -> unsecured ; 1 -> partial single ; >=2 -> multiple (partial_multi/fully)
    # (Proxy by count. Swap to value-vs-exposure if you supply those columns.)


cfg = Config()


# ==============================================================================
# 2. BRR HIERARCHY (1 = best/lowest risk … 22 = worst)
# ==============================================================================
BRR_HIERARCHY = [
    "1+", "1H", "1M", "1L",
    "2+H", "2+M", "2+L", "2H", "2M", "2L",
    "2-H", "2-M", "2-L",
    "3+H", "3+M", "3+L", "3H", "3M", "3L",
    "4", "5", "6",
]
_RANK = {r: i + 1 for i, r in enumerate(BRR_HIERARCHY)}


def _normalize_rating(value):
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() in {"nan", "null", "none", "blank"}:
        return None
    s = s.upper().replace(" ", "")
    if s in _RANK:
        return s
    m = re.match(r"^([0-9]+[+\-]?)([HML])?$", s)
    if m:
        cand = f"{m.group(1)}{m.group(2) or ''}"
        if cand in _RANK:
            return cand
    m2 = re.match(r"^([456])(\.0)?$", s)
    if m2 and m2.group(1) in _RANK:
        return m2.group(1)
    return None


def rating_rank(value):
    n = _normalize_rating(value)
    return _RANK.get(n) if n else None


def relationship(brr_value, gbrr_value):
    """better = lower rank (lower risk); worse = higher rank; equal = same."""
    rb, rg = rating_rank(brr_value), rating_rank(gbrr_value)
    if rb is None or rg is None:
        return None
    if rb < rg:
        return "better"
    if rb > rg:
        return "worse"
    return "equal"


# ==============================================================================
# 3. TEST CASE DEFINITIONS
#    securedness: unsecured | partial | partial_multi | fully | any
#    rel: better | equal | worse | None(any) | missing_brr | missing_gbrr
#    guarantee: True | False | "partial" | None
# ==============================================================================
TEST_CASES = [
    ("CMS_Batch_01", "Unsecured Borrower - BRR better than GBRR",              "unsecured",     "better",       True,      "Y"),
    ("CMS_Batch_02", "Unsecured Borrower - BRR worse than GBRR",               "unsecured",     "worse",        True,      "N"),
    ("CMS_Batch_03", "Partially secured Borrower + Single Collateral",         "partial",       None,           True,      "Y"),
    ("CMS_Batch_04", "Partially secured Borrower + Multiple Collateral",       "partial_multi", None,           True,      "Y"),
    ("CMS_Batch_05", "Partially secured + Multiple Collateral + BRR worse",    "partial_multi", "worse",        True,      "N"),
    ("CMS_Batch_06", "Fully secured Borrower + BRR better than GBRR",          "fully",         "better",       True,      "Y"),
    ("CMS_Batch_07", "Fully secured Borrower + BRR worse than GBRR",           "fully",         "worse",        True,      "N"),
    ("CMS_Batch_08", "BRR better than GBRR but partial guarantee",             "unsecured",     "better",       "partial", "Facility treated as unsecured"),
    ("CMS_Batch_09", "Unsecured Borrower - BRR equal to GBRR (Single)",        "unsecured",     "equal",        True,      "Y"),
    ("CMS_Batch_10", "Unsecured Borrower - BRR equal to GBRR (Multiple fac.)", "unsecured",     "equal",        True,      "Y"),
    ("CMS_Batch_11", "Partially secured - BRR equal to GBRR (Multiple)",       "partial_multi", "equal",        True,      "Y"),
    ("CMS_Batch_12", "Fully secured Borrower - BRR equal to GBRR (Multiple)",  "fully",         "equal",        True,      "Y"),
    ("CMS_Batch_13", "Unsec/Partial/Fully - BRR equal to GBRR (Multi Borr.)",  "any",           "equal",        True,      "mixed"),
    ("CMS_Batch_14", "Missing Borrower BRR",                                   "any",           "missing_brr",  True,      "Error"),
    ("CMS_Batch_15", "GBRR is NULL",                                           "any",           "missing_gbrr", True,      "Error"),
    ("CMS_Batch_16", "GBRR is BLANK",                                          "any",           "missing_gbrr", True,      "Error"),
]


# ==============================================================================
# 4. HELPERS
# ==============================================================================
def _has_value(x):
    if x is None:
        return False
    s = str(x).strip()
    return s != "" and s.lower() not in {"nan", "null", "none", "blank"}


AUDIT_COLS = [
    "_TestCaseNo", "_Scenario", "_SRF_Rank_In_Case",
    "_SRF_Source", "_MatchTriggeredThisRow",
    "_Orig_BorrowerBRR", "_Orig_GBRR", "_ExpectedIgnorePledge",
]


# ==============================================================================
# 5. SRF-LEVEL STRUCTURE
# ==============================================================================
def srf_collateral_count(group):
    col = cfg.COL_COLLATERAL
    if col not in group.columns:
        return 0
    return int(group[col].apply(_has_value).sum())


def srf_has_guarantee(group):
    gid, gtype = cfg.COL_GUARANTEE_ID, cfg.COL_GUAR_TYPE
    has = False
    if gid in group.columns:
        has = has or group[gid].apply(_has_value).any()
    if gtype in group.columns:
        has = has or group[gtype].apply(_has_value).any()
    return bool(has)


def srf_matches_securedness(group, secured_req):
    if secured_req == "any":
        return True
    n = srf_collateral_count(group)
    if secured_req == "unsecured":
        return n == 0
    if secured_req == "partial":
        return n == 1
    if secured_req == "partial_multi":
        return n >= 2
    if secured_req == "fully":
        return n >= 1
    return False


def srf_matches_guarantee(group, guar_req):
    if guar_req is None:
        return True
    has = srf_has_guarantee(group)
    if guar_req is True:
        return has
    if guar_req is False:
        return not has
    if guar_req == "partial":
        return has          # presence proxy for partial guarantee
    return True


def find_matching_row_index(group, rel):
    """
    Return the index label of the FIRST row in the SRF whose BorrowerBRR-vs-GBRR
    relationship equals `rel`. If rel is None/'mixed', return the first row.
    Returns None if no row matches.
    """
    if rel in (None, "mixed"):
        return group.index[0]
    for idx, row in group.iterrows():
        if relationship(row.get(cfg.COL_BRR), row.get(cfg.COL_GBRR)) == rel:
            return idx
    return None


# ==============================================================================
# 6. RELATIONSHIP FORCING / BORROWING
# ==============================================================================
def force_ratings_for_relationship(rel):
    if rel == "better":
        return "2H", "3H"
    if rel == "worse":
        return "3H", "2H"
    if rel == "equal":
        return "2H", "2H"
    return None, None


def donor_gbrr_for(rel, brr_value):
    rb = rating_rank(brr_value)
    if rb is None:
        return None
    if rel == "better":
        want = min(rb + 3, 22)
        if want <= rb:
            return None
    elif rel == "worse":
        want = max(rb - 3, 1)
        if want >= rb:
            return None
    elif rel == "equal":
        want = rb
    else:
        return None
    return BRR_HIERARCHY[want - 1]


# ==============================================================================
# 7. BUILD OUTPUT ROWS FOR A CHOSEN SRF
# ==============================================================================
def emit_srf(group, case, srf_rank, source, trigger_idx):
    """Return a DataFrame: all rows of this SRF + audit columns."""
    no, scenario, secured_req, rel, guar_req, expected = case
    out = group.copy()
    out["_TestCaseNo"] = no
    out["_Scenario"] = scenario
    out["_SRF_Rank_In_Case"] = srf_rank
    out["_SRF_Source"] = source
    out["_MatchTriggeredThisRow"] = [
        "YES" if idx == trigger_idx else "" for idx in out.index
    ]
    out["_Orig_BorrowerBRR"] = out.get(cfg.COL_BRR)
    out["_Orig_GBRR"] = out.get(cfg.COL_GBRR)
    out["_ExpectedIgnorePledge"] = expected
    return out


def apply_missing(group, case):
    """Handle CMS_Batch_14/15/16 — null/blank a rating on all rows of the SRF."""
    no, scenario, secured_req, rel, guar_req, expected = case
    out = group.copy()
    out["_Orig_BorrowerBRR"] = out.get(cfg.COL_BRR)
    out["_Orig_GBRR"] = out.get(cfg.COL_GBRR)
    if rel == "missing_brr":
        out[cfg.COL_BRR] = np.nan
        src = "FORCED(BRR->NULL)"
    else:
        out[cfg.COL_GBRR] = "" if no == "CMS_Batch_16" else np.nan
        src = "FORCED(GBRR->NULL/BLANK)" if no == "CMS_Batch_15" else "FORCED(GBRR->BLANK)"
    out["_TestCaseNo"] = no
    out["_Scenario"] = scenario
    out["_SRF_Source"] = src
    out["_MatchTriggeredThisRow"] = ""
    out["_ExpectedIgnorePledge"] = expected
    return out, src


def apply_borrow(group, case, trigger_idx):
    """Option A: swap GBRR on the matching row to force the relationship."""
    no, scenario, secured_req, rel, guar_req, expected = case
    out = group.copy()
    out["_Orig_BorrowerBRR"] = out.get(cfg.COL_BRR)
    out["_Orig_GBRR"] = out.get(cfg.COL_GBRR)

    brr = out.at[trigger_idx, cfg.COL_BRR]
    new_gbrr = donor_gbrr_for(rel, brr)
    if new_gbrr is None:
        return None, None
    orig = out.at[trigger_idx, cfg.COL_GBRR]
    out.at[trigger_idx, cfg.COL_GBRR] = new_gbrr

    out["_TestCaseNo"] = no
    out["_Scenario"] = scenario
    src = f"BORROWED(row GBRR {orig}->{new_gbrr})"
    out["_SRF_Source"] = src
    out["_MatchTriggeredThisRow"] = [
        "YES" if idx == trigger_idx else "" for idx in out.index
    ]
    out["_ExpectedIgnorePledge"] = expected
    return out, src


def apply_force(group, case):
    """Force clean BRR/GBRR on the first row of the SRF."""
    no, scenario, secured_req, rel, guar_req, expected = case
    out = group.copy()
    out["_Orig_BorrowerBRR"] = out.get(cfg.COL_BRR)
    out["_Orig_GBRR"] = out.get(cfg.COL_GBRR)
    fb, fg = force_ratings_for_relationship(rel)
    if fb is None:
        return None, None
    tgt = out.index[0]
    out.at[tgt, cfg.COL_BRR] = fb
    out.at[tgt, cfg.COL_GBRR] = fg
    out["_TestCaseNo"] = no
    out["_Scenario"] = scenario
    src = f"FORCED(BRR={fb},GBRR={fg})"
    out["_SRF_Source"] = src
    out["_MatchTriggeredThisRow"] = ["YES" if i == tgt else "" for i in out.index]
    out["_ExpectedIgnorePledge"] = expected
    return out, src


# ==============================================================================
# 8. PER-TEST-CASE SELECTION (SRF-group based)
# ==============================================================================
def select_for_case(srf_groups, ordered_srfs, case):
    no, scenario, secured_req, rel, guar_req, expected = case
    log.info(f"--- {no}: {scenario}")

    chosen = []          # list of output DataFrames
    natural_taken = 0

    # ---- Missing-value cases: pick structurally-eligible SRFs, then null/blank
    if rel in ("missing_brr", "missing_gbrr"):
        for srf in ordered_srfs:
            if len(chosen) >= cfg.SRF_PER_CASE:
                break
            g = srf_groups[srf]
            if srf_matches_guarantee(g, guar_req):   # securedness = 'any' here
                out, _ = apply_missing(g, case)
                out["_SRF_Rank_In_Case"] = len(chosen) + 1
                chosen.append(out)
        log.info(f"    emitted {len(chosen)} SRF(s)")
        return _finalize(chosen)

    # ---- PASS 1: natural SRFs (structure ok + a row already has the relationship)
    used = set()
    for srf in ordered_srfs:
        if len(chosen) >= cfg.SRF_PER_CASE:
            break
        g = srf_groups[srf]
        if not srf_matches_securedness(g, secured_req):
            continue
        if not srf_matches_guarantee(g, guar_req):
            continue
        trig = find_matching_row_index(g, rel)
        if trig is None:
            continue
        out = emit_srf(g, case, len(chosen) + 1, "NATURAL", trig)
        chosen.append(out)
        used.add(srf)
        natural_taken += 1
    log.info(f"    PASS1 natural SRFs: {natural_taken}")

    # ---- PASS 2: borrow (structure ok, force relationship on a matching row)
    if len(chosen) < cfg.SRF_PER_CASE and rel not in (None, "mixed"):
        for srf in ordered_srfs:
            if len(chosen) >= cfg.SRF_PER_CASE:
                break
            if srf in used:
                continue
            g = srf_groups[srf]
            if not srf_matches_securedness(g, secured_req):
                continue
            if not srf_matches_guarantee(g, guar_req):
                continue
            # pick any row to adjust (first row of the SRF)
            trig = g.index[0]
            out, src = apply_borrow(g, case, trig)
            if out is None:
                continue
            out["_SRF_Rank_In_Case"] = len(chosen) + 1
            chosen.append(out)
            used.add(srf)
        log.info(f"    after PASS2 borrow: {len(chosen)}")

    # ---- PASS 3: force clean values on a structurally-eligible SRF
    if len(chosen) < cfg.SRF_PER_CASE and rel not in (None, "mixed"):
        for srf in ordered_srfs:
            if len(chosen) >= cfg.SRF_PER_CASE:
                break
            if srf in used:
                continue
            g = srf_groups[srf]
            if not srf_matches_securedness(g, secured_req):
                continue
            if not srf_matches_guarantee(g, guar_req):
                continue
            out, src = apply_force(g, case)
            if out is None:
                continue
            out["_SRF_Rank_In_Case"] = len(chosen) + 1
            chosen.append(out)
            used.add(srf)
        log.info(f"    after PASS3 force: {len(chosen)}")

    # ---- rel is None / 'mixed': just take structurally-eligible SRFs
    if rel in (None, "mixed") and len(chosen) < cfg.SRF_PER_CASE:
        for srf in ordered_srfs:
            if len(chosen) >= cfg.SRF_PER_CASE:
                break
            if srf in used:
                continue
            g = srf_groups[srf]
            if not srf_matches_securedness(g, secured_req):
                continue
            if not srf_matches_guarantee(g, guar_req):
                continue
            trig = g.index[0]
            out = emit_srf(g, case, len(chosen) + 1, "NATURAL", trig)
            chosen.append(out)
            used.add(srf)

    if len(chosen) < cfg.SRF_PER_CASE:
        log.warning(f"    ONLY {len(chosen)} SRF(s) qualified for {no} "
                    f"(wanted {cfg.SRF_PER_CASE})")

    return _finalize(chosen)


def _finalize(chosen):
    if not chosen:
        return pd.DataFrame()
    return pd.concat(chosen, ignore_index=True)


# ==============================================================================
# 9. MAIN
# ==============================================================================
def main():
    ap = argparse.ArgumentParser(description="LGD SRF-group test data generator")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", default="testcase_testdata.xlsx")
    ap.add_argument("--srf-per-case", type=int, default=cfg.SRF_PER_CASE)
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--order", choices=["asc", "desc"], default="asc")
    args = ap.parse_args()

    cfg.SRF_PER_CASE = args.srf_per_case
    sheet = args.sheet if args.sheet is not None else cfg.INPUT_SHEET

    log.info(f"Reading {args.input} (sheet={sheet}) ...")
    try:
        df = pd.read_excel(args.input, sheet_name=sheet, dtype=str)
    except Exception as e:
        sys.exit(f"[FATAL] could not read input: {e}")
    log.info(f"Loaded {len(df):,} rows x {len(df.columns)} cols")

    if cfg.COL_SRF not in df.columns:
        sys.exit(f"[FATAL] grouping column '{cfg.COL_SRF}' not found. "
                 f"Columns start with: {list(df.columns)[:8]}")

    for c in (cfg.COL_BRR, cfg.COL_GBRR):
        if c not in df.columns:
            log.warning(f"Column '{c}' not found — check Config header names.")

    _report_unmapped_ratings(df)

    # group by SRF, preserve original columns/order
    original_cols = list(df.columns)
    srf_groups = {srf: g for srf, g in df.groupby(cfg.COL_SRF, sort=False)}

    # order the SRF keys ascending/descending (numeric-aware)
    def _key(v):
        try:
            return (0, float(str(v)))
        except (ValueError, TypeError):
            return (1, str(v))
    ordered_srfs = sorted(srf_groups.keys(), key=_key,
                          reverse=(args.order == "desc"))
    log.info(f"Total SRF groups: {len(ordered_srfs):,} (order={args.order})")

    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        summary = []
        for case in TEST_CASES:
            out = select_for_case(srf_groups, ordered_srfs, case)
            if out.empty:
                # still write an empty sheet with headers
                out = pd.DataFrame(columns=original_cols + AUDIT_COLS)
            ordered_cols = original_cols + [c for c in AUDIT_COLS if c in out.columns]
            # ensure all audit cols exist
            for c in AUDIT_COLS:
                if c not in out.columns:
                    out[c] = ""
            out = out.reindex(columns=original_cols + AUDIT_COLS)
            out.to_excel(writer, sheet_name=case[0][:31], index=False)

            n_srf = out["_TestCaseNo"].notna().sum() and out.get("_SRF_Rank_In_Case")
            distinct_srf = (
                out[cfg.COL_SRF].nunique() if cfg.COL_SRF in out.columns and not out.empty else 0
            )
            summary.append({
                "TestCaseNo": case[0],
                "Scenario": case[1],
                "SRFs_Emitted": distinct_srf,
                "Total_Rows": len(out),
                "Sources": ", ".join(sorted(set(
                    str(s) for s in out.get("_SRF_Source", pd.Series(dtype=str)).dropna().unique()
                ))) if not out.empty else "",
                "ExpectedIgnorePledge": case[5],
            })
        pd.DataFrame(summary).to_excel(writer, sheet_name="_SUMMARY", index=False)

    log.info(f"Wrote {args.output}")
    print(f"\nDone. Output: {args.output}")


def _report_unmapped_ratings(df):
    bad = set()
    for col in (cfg.COL_BRR, cfg.COL_GBRR):
        if col in df.columns:
            for v in df[col].dropna().unique():
                if _has_value(v) and rating_rank(v) is None:
                    bad.add(str(v))
    if bad:
        log.warning(f"Unmapped rating values: "
                    f"{sorted(bad)[:20]}{' ...' if len(bad) > 20 else ''}")


if __name__ == "__main__":
    main()
