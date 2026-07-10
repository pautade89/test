"""
================================================================================
 LGD ENGINE — TESTCASE-WISE TEST DATA GENERATOR  (VECTORIZED / 800k-ROW EDITION)
================================================================================
 Same selection logic as the SRF-group edition, re-engineered for ~800k rows:

   * VECTORIZED securedness + guarantee + BRR/GBRR relationship (no per-row
     Python loops over the full dataset — all done with pandas/numpy in C).
   * TWO-PHASE read to save memory:
       Phase 1  read ONLY the columns needed to DECIDE which SRFs qualify
                (SRF, BRR, GBRR, CollateralID, GuaranteeID, GuarantorType,
                 FacilityID), classify every SRF, and choose up to 2 SRFs/case.
       Phase 2  read the FULL 97-column rows for ONLY the chosen SRFs
                (~32 SRFs total), so the wide frame is tiny in memory.
   * CSV-first (chunked scan supported); .xlsx also supported.

 Selection unit = SingleNameSRF group (whole exposure). For each of the 16
 batch test cases, evaluate SRFs in ascending SingleNameSRF order; an SRF
 qualifies when securedness (SRF level) + guarantee (SRF level) match AND at
 least one row has the required BorrowerBRR-vs-GBRR relationship (row level).
 Copy ALL rows of up to 2 qualifying SRFs, all original columns + audit columns.

 RUN:
   python lgd_testdata_gen_fast.py \
       --input cms_consolidated.csv \
       --output testcase_testdata.xlsx \
       --srf-per-case 2 --order asc
================================================================================
"""

import argparse
import logging
import sys
import re
import os

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("lgd_fast")


# ==============================================================================
# 1. CONFIG
# ==============================================================================
class Config:
    COL_SRF          = "SingleNameSRF"
    COL_BRR          = "BorrowerBRR"
    COL_GBRR         = "GBRR"
    COL_COLLATERAL   = "CollateralID"
    COL_FACILITY     = "FacilityID"
    COL_GUARANTEE_ID = "GuaranteeID"
    COL_GUAR_TYPE    = "GuarantorType"

    SRF_PER_CASE = 2
    INPUT_SHEET  = 0

    # Columns needed for the DECISION phase (Phase 1). Keep this minimal.
    DECISION_COLS = [
        COL_SRF, COL_BRR, COL_GBRR, COL_COLLATERAL,
        COL_FACILITY, COL_GUARANTEE_ID, COL_GUAR_TYPE,
    ]

    # CSV read options
    CSV_ENCODING = None      # e.g. "utf-8-sig" or "cp1252" if needed
    CSV_SEP      = ","       # e.g. ";" or "\t"
    CSV_CHUNK    = 200_000   # rows per chunk in Phase 1 scan

    # --- OUTPUT COLUMN FILTER --------------------------------------------------
    # Only these original columns are written to the output (in this order),
    # followed by the audit columns. Set OUTPUT_COLS = None to keep ALL columns.
    # Names must match the consolidated sheet headers exactly. Any name not
    # present in the file is skipped with a warning (so it's safe to list extras).
    OUTPUT_COLS = [
        # --- Single name / borrower ---
        "SingleNameSRF",            # SRF (single name)
        "BorrowerSRF",              # Borrower
        "BorrowerBRR",              # BRR
        "BorrowerSectorTypeCd",     # sector (drives loss-driver rules)
        "BorrowerOSCAD",            # borrower outstanding (CAD)
        "BorrowerAuthLimitCAD",     # borrower authorized amount (CAD)
        "AuthorizedLimit",          # (cms) authorized limit, if present
        # --- Facility ---
        "FacilityID",               # facility id
        "CADAdjAuthAmt",            # facility authorized (CAD adj auth amt)
        "CADOSBalance",             # facility outstanding (CAD O/S balance)
        "SeniorityCd",              # seniority
        "FinalSegmentID",           # final segment id
        "FinalLGDRate",             # final LGD rate
        "FinalEAD",                 # EAD
        "FinalUGD",                 # UGD
        # --- Collateral ---
        "CollateralID",             # collateral id
        "AllocationValue",          # allocation value
        "EligibleCollateralAmountCAD",  # eligible collateral amount
        # --- Guarantee / guarantor ---
        "GuarantorSRF",             # guarantor
        "GuarantorType",            # guarantor type
        "GBRR",                     # GBRR
        "SupportedByDocumentId",    # guarantor document id
        # --- Result field ---
        "IgnorePledgeIndicator",    # DE0137 (expected result driver)
    ]


cfg = Config()


# ==============================================================================
# 2. BRR HIERARCHY (1 best … 22 worst) + VECTORIZED rank mapping
# ==============================================================================
BRR_HIERARCHY = [
    "1+", "1H", "1M", "1L",
    "2+H", "2+M", "2+L", "2H", "2M", "2L",
    "2-H", "2-M", "2-L",
    "3+H", "3+M", "3+L", "3H", "3M", "3L",
    "4", "5", "6",
]
_RANK = {r: i + 1 for i, r in enumerate(BRR_HIERARCHY)}


def _norm_scalar(s):
    if s is None:
        return None
    s = str(s).strip()
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


def rank_series(series):
    """Vectorized: map a rating Series -> Int rank (NaN if unmapped)."""
    # normalize via a cached unique-value map (fast for large data)
    uniques = series.dropna().unique()
    lut = {u: _RANK.get(_norm_scalar(u)) for u in uniques}
    return series.map(lut)


def rating_rank(value):
    n = _norm_scalar(value)
    return _RANK.get(n) if n else None


# ==============================================================================
# 3. TEST CASES
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

AUDIT_COLS = [
    "_TestCaseNo", "_Scenario", "_SRF_Rank_In_Case",
    "_SRF_Source", "_MatchTriggeredThisRow",
    "_Orig_BorrowerBRR", "_Orig_GBRR", "_ExpectedIgnorePledge",
]


# ==============================================================================
# 4. VECTORIZED "has value" for a whole Series
# ==============================================================================
def has_value_series(s):
    if s is None:
        return None
    ss = s.astype(str).str.strip()
    empty = ss.eq("") | ss.str.lower().isin(["nan", "null", "none", "blank"])
    return ~empty


# ==============================================================================
# 5. PHASE 1 — read decision columns, classify every SRF (vectorized)
# ==============================================================================
def read_decision_frame(path, is_csv, sheet):
    """Read only the decision columns. CSV uses chunked concat to cap memory."""
    if is_csv:
        chunks = []
        reader = pd.read_csv(
            path, dtype=str, usecols=lambda c: c in cfg.DECISION_COLS,
            encoding=cfg.CSV_ENCODING, sep=cfg.CSV_SEP,
            keep_default_na=False, chunksize=cfg.CSV_CHUNK,
        )
        for i, ch in enumerate(reader):
            chunks.append(ch)
            log.info(f"    scanned chunk {i+1} ({len(ch):,} rows)")
        return pd.concat(chunks, ignore_index=True)
    else:
        # xlsx: can't chunk; read decision cols only
        return pd.read_excel(path, sheet_name=sheet, dtype=str,
                             usecols=lambda c: c in cfg.DECISION_COLS)


def classify_srfs(dec):
    """
    Build a per-row helper frame + per-SRF classification, all vectorized.
    Returns:
      dec  : decision frame augmented with _rel, _has_coll, _has_guar, _brr_rank, _gbrr_rank
      srf_info : DataFrame indexed by SRF with columns:
                 coll_count, has_guar, secured_class
    """
    srf = cfg.COL_SRF

    # vectorized ranks + relationship
    dec["_brr_rank"] = rank_series(dec[cfg.COL_BRR])
    dec["_gbrr_rank"] = rank_series(dec[cfg.COL_GBRR])

    rb = dec["_brr_rank"]
    rg = dec["_gbrr_rank"]
    rel = np.where(rb.isna() | rg.isna(), None,
          np.where(rb < rg, "better",
          np.where(rb > rg, "worse", "equal")))
    dec["_rel"] = rel

    # per-row structural signals
    dec["_has_coll"] = has_value_series(dec[cfg.COL_COLLATERAL]) if cfg.COL_COLLATERAL in dec else False
    hg = pd.Series(False, index=dec.index)
    if cfg.COL_GUARANTEE_ID in dec:
        hg = hg | has_value_series(dec[cfg.COL_GUARANTEE_ID])
    if cfg.COL_GUAR_TYPE in dec:
        hg = hg | has_value_series(dec[cfg.COL_GUAR_TYPE])
    dec["_has_guar_row"] = hg

    # per-SRF aggregates (one C-level pass each)
    g = dec.groupby(srf, sort=False)
    coll_count = g["_has_coll"].sum().astype(int)
    has_guar = g["_has_guar_row"].any()

    srf_info = pd.DataFrame({"coll_count": coll_count, "has_guar": has_guar})

    # securedness class from count
    def _sec(n):
        if n == 0:
            return "unsecured"
        if n == 1:
            return "partial"      # single
        return "multiple"         # >=2  (covers partial_multi & fully)
    srf_info["secured_class"] = srf_info["coll_count"].map(_sec)

    return dec, srf_info


def secured_ok(secured_req, secured_class, coll_count):
    if secured_req == "any":
        return True
    if secured_req == "unsecured":
        return secured_class == "unsecured"
    if secured_req == "partial":
        return coll_count == 1
    if secured_req == "partial_multi":
        return coll_count >= 2
    if secured_req == "fully":
        return coll_count >= 1
    return False


def guar_ok(guar_req, has_guar):
    if guar_req is None:
        return True
    if guar_req is True or guar_req == "partial":
        return bool(has_guar)
    if guar_req is False:
        return not bool(has_guar)
    return True


# ==============================================================================
# 6. CHOOSE SRFs PER CASE (works on the small per-SRF summary, not full data)
# ==============================================================================
def choose_srfs(dec, srf_info, ordered_srfs, case):
    """
    Return a list of tuples: (srf, source_label, trigger_relationship)
    describing up to SRF_PER_CASE chosen SRFs for this case.
    Uses vectorized per-SRF relationship availability.
    """
    no, scenario, secured_req, rel, guar_req, expected = case

    # Precompute, per SRF, which relationships are available among its rows.
    # Build once per case call is wasteful; caller passes a shared structure.
    rel_by_srf = case_rel_lookup  # module-level cache set in main()

    chosen = []
    used = set()

    # Missing-value cases: any structurally/guarantee-eligible SRF works
    if rel in ("missing_brr", "missing_gbrr"):
        for srf in ordered_srfs:
            if len(chosen) >= cfg.SRF_PER_CASE:
                break
            info = srf_info.loc[srf]
            if guar_ok(guar_req, info["has_guar"]):
                chosen.append((srf, "MISSING", None))
        return chosen

    # PASS 1 — natural
    for srf in ordered_srfs:
        if len(chosen) >= cfg.SRF_PER_CASE:
            break
        info = srf_info.loc[srf]
        if not secured_ok(secured_req, info["secured_class"], info["coll_count"]):
            continue
        if not guar_ok(guar_req, info["has_guar"]):
            continue
        if rel in (None, "mixed"):
            chosen.append((srf, "NATURAL", None)); used.add(srf); continue
        if rel in rel_by_srf.get(srf, ()):
            chosen.append((srf, "NATURAL", rel)); used.add(srf)

    # PASS 2 — borrow (structure ok, relationship forced on one row)
    if len(chosen) < cfg.SRF_PER_CASE and rel not in (None, "mixed"):
        for srf in ordered_srfs:
            if len(chosen) >= cfg.SRF_PER_CASE:
                break
            if srf in used:
                continue
            info = srf_info.loc[srf]
            if not secured_ok(secured_req, info["secured_class"], info["coll_count"]):
                continue
            if not guar_ok(guar_req, info["has_guar"]):
                continue
            chosen.append((srf, "BORROW", rel)); used.add(srf)

    # PASS 3 — force
    if len(chosen) < cfg.SRF_PER_CASE and rel not in (None, "mixed"):
        for srf in ordered_srfs:
            if len(chosen) >= cfg.SRF_PER_CASE:
                break
            if srf in used:
                continue
            info = srf_info.loc[srf]
            if not secured_ok(secured_req, info["secured_class"], info["coll_count"]):
                continue
            if not guar_ok(guar_req, info["has_guar"]):
                continue
            chosen.append((srf, "FORCE", rel)); used.add(srf)

    # rel None/mixed fallback already handled in PASS1
    return chosen


# ==============================================================================
# 7. PHASE 2 — pull FULL rows for chosen SRFs only, then apply edits
# ==============================================================================
def read_full_rows_for(path, is_csv, sheet, wanted_srfs):
    """Read all columns but keep only rows whose SRF is in wanted_srfs."""
    srf = cfg.COL_SRF
    wanted = set(str(x) for x in wanted_srfs)
    if is_csv:
        out = []
        reader = pd.read_csv(path, dtype=str, encoding=cfg.CSV_ENCODING,
                             sep=cfg.CSV_SEP, keep_default_na=False,
                             chunksize=cfg.CSV_CHUNK)
        for ch in reader:
            keep = ch[ch[srf].astype(str).isin(wanted)]
            if not keep.empty:
                out.append(keep)
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    else:
        full = pd.read_excel(path, sheet_name=sheet, dtype=str)
        return full[full[srf].astype(str).isin(wanted)].copy()


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


def force_ratings(rel):
    return {"better": ("2H", "3H"), "worse": ("3H", "2H"),
            "equal": ("2H", "2H")}.get(rel, (None, None))


def build_case_sheet(full_map, case, chosen, original_cols):
    """Assemble the output rows (all cols + audit) for one test case."""
    no, scenario, secured_req, rel, guar_req, expected = case
    frames = []

    for rank_i, (srf, source, trig_rel) in enumerate(chosen, start=1):
        grp = full_map.get(str(srf))
        if grp is None or grp.empty:
            continue
        out = grp.copy()
        out["_Orig_BorrowerBRR"] = out.get(cfg.COL_BRR)
        out["_Orig_GBRR"] = out.get(cfg.COL_GBRR)
        out["_TestCaseNo"] = no
        out["_Scenario"] = scenario
        out["_SRF_Rank_In_Case"] = rank_i
        out["_ExpectedIgnorePledge"] = expected
        out["_MatchTriggeredThisRow"] = ""

        if source == "MISSING":
            if rel == "missing_brr":
                out[cfg.COL_BRR] = np.nan
                out["_SRF_Source"] = "FORCED(BRR->NULL)"
            else:
                out[cfg.COL_GBRR] = "" if no == "CMS_Batch_16" else np.nan
                out["_SRF_Source"] = ("FORCED(GBRR->BLANK)" if no == "CMS_Batch_16"
                                      else "FORCED(GBRR->NULL)")

        elif source == "NATURAL":
            out["_SRF_Source"] = "NATURAL"
            if trig_rel not in (None, "mixed"):
                # mark first row that already satisfies the relationship
                rb = rank_series(out[cfg.COL_BRR])
                rg = rank_series(out[cfg.COL_GBRR])
                if trig_rel == "better":
                    mask = rb < rg
                elif trig_rel == "worse":
                    mask = rb > rg
                else:
                    mask = rb == rg
                idxs = out.index[mask.fillna(False)]
                if len(idxs):
                    out.loc[idxs[0], "_MatchTriggeredThisRow"] = "YES"

        elif source == "BORROW":
            tgt = out.index[0]
            brr = out.at[tgt, cfg.COL_BRR]
            new_g = donor_gbrr_for(rel, brr)
            if new_g is None:
                # fall back to force if borrow impossible for this BRR
                fb, fg = force_ratings(rel)
                out.at[tgt, cfg.COL_BRR] = fb
                out.at[tgt, cfg.COL_GBRR] = fg
                out["_SRF_Source"] = f"FORCED(BRR={fb},GBRR={fg})"
            else:
                orig = out.at[tgt, cfg.COL_GBRR]
                out.at[tgt, cfg.COL_GBRR] = new_g
                out["_SRF_Source"] = f"BORROWED(row GBRR {orig}->{new_g})"
            out.at[tgt, "_MatchTriggeredThisRow"] = "YES"

        elif source == "FORCE":
            tgt = out.index[0]
            fb, fg = force_ratings(rel)
            out.at[tgt, cfg.COL_BRR] = fb
            out.at[tgt, cfg.COL_GBRR] = fg
            out["_SRF_Source"] = f"FORCED(BRR={fb},GBRR={fg})"
            out.at[tgt, "_MatchTriggeredThisRow"] = "YES"

        frames.append(out)

    if not frames:
        return pd.DataFrame(columns=original_cols + AUDIT_COLS)
    res = pd.concat(frames, ignore_index=True)
    return res.reindex(columns=original_cols + AUDIT_COLS)


# ==============================================================================
# 8. MAIN
# ==============================================================================
case_rel_lookup = {}   # module-level cache: srf -> set of available relationships


def main():
    ap = argparse.ArgumentParser(description="LGD fast SRF test data generator")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", default="testcase_testdata.xlsx")
    ap.add_argument("--srf-per-case", type=int, default=cfg.SRF_PER_CASE)
    ap.add_argument("--order", choices=["asc", "desc"], default="asc")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--csv-sep", default=None)
    ap.add_argument("--csv-encoding", default=None)
    args = ap.parse_args()

    cfg.SRF_PER_CASE = args.srf_per_case
    if args.csv_sep:
        cfg.CSV_SEP = args.csv_sep
    if args.csv_encoding:
        cfg.CSV_ENCODING = args.csv_encoding
    sheet = args.sheet if args.sheet is not None else cfg.INPUT_SHEET
    is_csv = args.input.lower().endswith(".csv")

    log.info(f"Input: {args.input} ({'CSV' if is_csv else 'Excel'})")

    # ---- PHASE 1: decision scan ----
    log.info("PHASE 1: scanning decision columns ...")
    dec = read_decision_frame(args.input, is_csv, sheet)
    log.info(f"  decision frame: {len(dec):,} rows x {len(dec.columns)} cols")

    if cfg.COL_SRF not in dec.columns:
        sys.exit(f"[FATAL] '{cfg.COL_SRF}' not found. Check header names in Config.")

    dec, srf_info = classify_srfs(dec)
    log.info(f"  classified {len(srf_info):,} SRF groups")

    # per-SRF set of available relationships (vectorized groupby)
    global case_rel_lookup
    rel_nonnull = dec[dec["_rel"].notna()]
    case_rel_lookup = (
        rel_nonnull.groupby(cfg.COL_SRF)["_rel"].agg(lambda s: set(s)).to_dict()
    )

    # order SRFs numeric-aware
    def _key(v):
        try:
            return (0, float(str(v)))
        except (ValueError, TypeError):
            return (1, str(v))
    ordered_srfs = sorted(srf_info.index.tolist(), key=_key,
                          reverse=(args.order == "desc"))

    # ---- choose SRFs for every case (tiny work on the summary) ----
    plan = {}
    wanted_all = set()
    for case in TEST_CASES:
        chosen = choose_srfs(dec, srf_info, ordered_srfs, case)
        plan[case[0]] = (case, chosen)
        for srf, _, _ in chosen:
            wanted_all.add(str(srf))
        if len(chosen) < cfg.SRF_PER_CASE:
            log.warning(f"  {case[0]}: only {len(chosen)} SRF(s) qualified")
        else:
            log.info(f"  {case[0]}: chose {len(chosen)} SRF(s)")

    log.info(f"Total distinct SRFs to pull: {len(wanted_all)}")

    # free decision frame memory before the wide read
    del dec
    import gc; gc.collect()

    # ---- PHASE 2: pull full-width rows for chosen SRFs only ----
    log.info("PHASE 2: reading full rows for chosen SRFs ...")
    full = read_full_rows_for(args.input, is_csv, sheet, wanted_all)
    if full.empty:
        sys.exit("[FATAL] no rows pulled in phase 2 — check SRF matching.")
    # --- decide which original columns to KEEP in the output ---
    if cfg.OUTPUT_COLS is None:
        original_cols = list(full.columns)
    else:
        present = [c for c in cfg.OUTPUT_COLS if c in full.columns]
        missing = [c for c in cfg.OUTPUT_COLS if c not in full.columns]
        if missing:
            log.warning(f"  OUTPUT_COLS not found in file (skipped): {missing}")
        # always ensure the SRF key and rating cols are present for internal use
        for must in (cfg.COL_SRF, cfg.COL_BRR, cfg.COL_GBRR):
            if must not in present and must in full.columns:
                present.append(must)
        original_cols = present
        log.info(f"  keeping {len(original_cols)} output columns")

    full_map = {str(srf): g for srf, g in full.groupby(cfg.COL_SRF, sort=False)}
    log.info(f"  pulled {len(full):,} rows across {len(full_map)} SRFs")

    # ---- write output ----
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        summary = []
        for case in TEST_CASES:
            c, chosen = plan[case[0]]
            sheet_df = build_case_sheet(full_map, c, chosen, original_cols)
            sheet_df.to_excel(writer, sheet_name=case[0][:31], index=False)
            distinct = sheet_df[cfg.COL_SRF].nunique() if not sheet_df.empty else 0
            srcs = ", ".join(sorted(set(
                str(s) for s in sheet_df.get("_SRF_Source", pd.Series(dtype=str)).dropna().unique()
            ))) if not sheet_df.empty else ""
            summary.append({
                "TestCaseNo": case[0], "Scenario": case[1],
                "SRFs_Emitted": distinct, "Total_Rows": len(sheet_df),
                "Sources": srcs, "ExpectedIgnorePledge": case[5],
            })
        pd.DataFrame(summary).to_excel(writer, sheet_name="_SUMMARY", index=False)

    log.info(f"Wrote {args.output}")
    print(f"\nDone. Output: {args.output}")


if __name__ == "__main__":
    main()
