#!/usr/bin/env python3
"""
v2_study.py -- run the pre-registered V2 variants and apply the selection rule
exactly as written in V2_RESEARCH.md.

    python v2_study.py                 # TRAIN + VALIDATE, all variants, selection
    python v2_study.py --final H4_90   # the ONE holdout run for the final pick

Each run goes through run_backtest.py with an override JSON (overrides/v2_<id>.json),
so outputs are byte-for-byte what a normal run would produce:
    results/v2/<WINDOW>/<id>[_favfirst]/{trades,executions,events}.csv, metrics.json, config.json
Summary: results/v2/summary.csv (one row per window x variant x ordering).

The HOLDOUT-P window is refused unless --final is given, and --final refuses
to run twice (results/v2/HOLDOUT-P/ must not exist yet).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ORDERINGS = (("adverse-first", ""), ("favourable-first", "_favfirst"))

# --- variants, verbatim from V2_RESEARCH.md ----------------------------------------------
VARIANTS: Dict[str, dict] = {
    "V1": {},
    "H1_22k": {"distance_scale_ref": 22000},
    "H1_24k": {"distance_scale_ref": 24000},
    "H1_26k": {"distance_scale_ref": 26000},
    "H2": {"extreme_tracking": "FLAT_ONLY"},
    "H3": {"breaker_enabled": False},
    "H4_75": {"early_stop_distance": 75, "early_arm_distance": 75},
    "H4_90": {"early_stop_distance": 90, "early_arm_distance": 90},
    "H4_120": {"early_stop_distance": 120, "early_arm_distance": 120},
    "H4_150": {"early_stop_distance": 150, "early_arm_distance": 150},
    "H5": {"gap_applies_when_flat": True},
    "S150": {"swing_distance": 150, "trail_distance": 150},
    "S210": {"swing_distance": 210, "trail_distance": 210},
    "S240": {"swing_distance": 240, "trail_distance": 240},
}
FAMILY = {"H1_24k": "H1", "H2": "H2", "H3": "H3", "H4_90": "H4", "H4_120": "H4", "H5": "H5"}
PROMOTABLE = list(FAMILY)
NEIGHBOURS = {"H1_24k": ("H1_22k", "H1_26k"), "H4_90": ("H4_75", "H4_120"), "H4_120": ("H4_90", "H4_150")}
STUDY_WINDOWS = ("TRAIN", "VALIDATE")


# --- running ---------------------------------------------------------------------------------
def override_path(vid: str, ov: dict) -> str:
    p = os.path.join(HERE, "overrides", f"v2_{vid}.json")
    with open(p, "w") as fh:
        json.dump(ov, fh, indent=2, sort_keys=True)
    return p


def run_one(window: str, vid: str, ov: dict, suffix: str) -> str:
    out = os.path.join(HERE, "results", "v2", window, vid + suffix)
    os.makedirs(out, exist_ok=True)
    data = os.path.join(HERE, "data", f"proxy_{window}{suffix}.csv")
    if not os.path.exists(data):
        raise SystemExit(f"missing {data} -- run make_proxy_data.py first")
    with open(os.path.join(out, "run.log"), "w") as fh:
        rc = subprocess.call([PY, os.path.join(HERE, "run_backtest.py"), "--merged", data,
                              "--config", override_path(vid, ov), "--out", out],
                             stdout=fh, stderr=subprocess.STDOUT)
    if rc != 0:
        raise SystemExit(f"run failed: {window}/{vid}{suffix} (see {out}/run.log)")
    return out


# --- metrics ---------------------------------------------------------------------------------
def sequences(t: pd.DataFrame) -> pd.Series:
    seq, out = 0, []
    for r in t.sort_values(["entry_datetime", "trade_id"]).itertuples():
        if r.entry_reason in ("ENTRY", "ENTRY_PENDING") or seq == 0:
            seq += 1
        out.append((r.Index, seq))
    return pd.Series(dict(out))


def summarise(window: str, vid: str, ordering: str, out: str) -> dict:
    m = json.load(open(os.path.join(out, "metrics.json")))
    try:
        t = pd.read_csv(os.path.join(out, "trades.csv"), parse_dates=["entry_datetime", "exit_datetime"])
    except pd.errors.EmptyDataError:
        t = pd.DataFrame()
    row = {"window": window, "variant": vid, "ordering": ordering,
           "net": m.get("total_net_pnl", 0.0), "max_dd": m.get("maximum_drawdown", 0.0),
           "trades": m.get("completed_trades", 0), "win_rate": m.get("win_rate"),
           "profit_factor": m.get("profit_factor"), "decisions": 0, "net_pct_sum": 0.0,
           "worst_sequence": 0.0, "profitable_quarters": None}
    if len(t):
        t = t[t["exit_reason"] != "DATASET_END"].copy()
        if len(t):
            t["seq"] = sequences(t)
            row["decisions"] = int(t["entry_reason"].isin(["ENTRY", "ENTRY_PENDING"]).sum())
            row["net_pct_sum"] = float((t["gross_pnl"] / t["entry_qty"] / t["entry_spot"]).sum() * 100)
            row["worst_sequence"] = float(t.groupby("seq")["net_pnl"].sum().min())
            q = t.groupby(t["exit_datetime"].dt.to_period("Q"))["net_pnl"].sum()
            row["profitable_quarters"] = float((q > 0).mean())
            for y, v in t.groupby(t["exit_datetime"].dt.year)["net_pnl"].sum().items():
                row[f"net_{y}"] = float(v)
    return row


# --- selection rule (V2_RESEARCH.md, verbatim) -------------------------------------------------
def select(df: pd.DataFrame) -> dict:
    def get(window, vid, ordering, col):
        r = df[(df.window == window) & (df.variant == vid) & (df.ordering == ordering)]
        return float(r.iloc[0][col]) if len(r) else float("nan")

    ords = [o for o, _ in ORDERINGS]
    report, promoted = {}, []
    for v in PROMOTABLE:
        a = all(get("VALIDATE", v, o, "net") > get("VALIDATE", "V1", o, "net")
                and get("VALIDATE", v, o, "max_dd") < get("VALIDATE", "V1", o, "max_dd") for o in ords)
        b = all(get("VALIDATE", n, o, "net") > get("VALIDATE", "V1", o, "net")
                for n in NEIGHBOURS.get(v, ()) for o in ords)
        c = all(get("TRAIN", v, o, "net") >= get("TRAIN", "V1", o, "net") for o in ords)
        ok = a and b and c
        report[v] = {"a_validate_beats_v1_net_and_dd": a, "b_neighbours_beat_v1": b if v in NEIGHBOURS else "n/a",
                     "c_train_not_worse": c, "promoted": ok,
                     "validate_net_avg": sum(get("VALIDATE", v, o, "net") for o in ords) / 2}
        if ok:
            promoted.append(v)
    return {"per_variant": report, "promoted": promoted}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", help="variant id for the single HOLDOUT-P run")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    os.makedirs(os.path.join(HERE, "results", "v2"), exist_ok=True)

    if args.final:
        return final(args.final)

    jobs = [(w, vid, ov, o, sfx) for w in STUDY_WINDOWS for vid, ov in VARIANTS.items() for o, sfx in ORDERINGS]
    print(f"running {len(jobs)} backtests ({len(VARIANTS)} variants x {len(STUDY_WINDOWS)} windows x 2 orderings)")
    with ThreadPoolExecutor(args.workers) as ex:
        outs = list(ex.map(lambda j: (j, run_one(j[0], j[1], j[2], j[4])), jobs))
    rows = [summarise(w, vid, o, out) for (w, vid, ov, o, sfx), out in outs]
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(HERE, "results", "v2", "summary.csv"), index=False)

    show = ["variant", "ordering", "net", "max_dd", "trades", "decisions", "win_rate", "profit_factor",
            "worst_sequence", "profitable_quarters"]
    for w in STUDY_WINDOWS:
        print(f"\n=== {w} ===")
        print(df[df.window == w][show].to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    sel = select(df)
    with open(os.path.join(HERE, "results", "v2", "selection.json"), "w") as fh:
        json.dump(sel, fh, indent=2, default=str)
    print("\n=== selection rule (V2_RESEARCH.md) ===")
    for v, r in sel["per_variant"].items():
        print(f"  {v:7} (a) {str(r['a_validate_beats_v1_net_and_dd']):5}  (b) {str(r['b_neighbours_beat_v1']):5}"
              f"  (c) {str(r['c_train_not_worse']):5}  -> {'PROMOTED' if r['promoted'] else 'not promoted'}")
    print(f"\npromoted: {sel['promoted'] or 'none'}")

    # --- combination (pre-registered; strictest reading: ONE pair = top 2 promoted
    #     variants from different hypothesis families, by VALIDATE net averaged over orderings)
    pv = sel["per_variant"]
    ranked = sorted(sel["promoted"], key=lambda v: -pv[v]["validate_net_avg"])
    pair, fams = [], set()
    for v in ranked:
        if FAMILY[v] not in fams:
            pair.append(v); fams.add(FAMILY[v])
        if len(pair) == 2:
            break
    candidates = {v: pv[v]["validate_net_avg"] for v in sel["promoted"]}
    if len(pair) == 2:
        cid = "C_" + "+".join(pair)
        cov = {**VARIANTS[pair[0]], **VARIANTS[pair[1]]}
        crow = []
        for w in STUDY_WINDOWS:
            for o, sfx in ORDERINGS:
                crow.append(summarise(w, cid, o, run_one(w, cid, cov, sfx)))
        df = pd.concat([df, pd.DataFrame(crow)], ignore_index=True)
        df.to_csv(os.path.join(HERE, "results", "v2", "summary.csv"), index=False)
        g = lambda w, v, o, c: float(df[(df.window == w) & (df.variant == v) & (df.ordering == o)].iloc[0][c])
        ords = [o for o, _ in ORDERINGS]
        a = all(g("VALIDATE", cid, o, "net") > g("VALIDATE", "V1", o, "net")
                and g("VALIDATE", cid, o, "max_dd") < g("VALIDATE", "V1", o, "max_dd") for o in ords)
        c = all(g("TRAIN", cid, o, "net") >= g("TRAIN", "V1", o, "net") for o in ords)
        cavg = sum(g("VALIDATE", cid, o, "net") for o in ords) / 2
        beats = all(cavg > pv[v]["validate_net_avg"] for v in pair)
        ok = a and c and beats
        sel["combination"] = {"id": cid, "overrides": cov, "a": a, "c": c, "beats_components": beats,
                              "validate_net_avg": cavg, "promoted": ok}
        print(f"\ncombination {cid}: (a) {a}  (c) {c}  beats components {beats} -> {'PROMOTED' if ok else 'not promoted'}")
        if ok:
            candidates[cid] = cavg
            with open(os.path.join(HERE, "overrides", f"v2_{cid}.json"), "w") as fh:
                json.dump(cov, fh, indent=2, sort_keys=True)
    sel["final_pick"] = max(candidates, key=candidates.get) if candidates else None
    with open(os.path.join(HERE, "results", "v2", "selection.json"), "w") as fh:
        json.dump(sel, fh, indent=2, default=str)
    print(f"final pick for the single HOLDOUT-P run: {sel['final_pick'] or 'NONE -- V1 stays the only model'}")
    return 0


def final(vid: str) -> int:
    if os.path.exists(os.path.join(HERE, "results", "v2", "HOLDOUT-P")):
        raise SystemExit("results/v2/HOLDOUT-P already exists -- the holdout is looked at once")
    ov = VARIANTS.get(vid) or json.load(open(os.path.join(HERE, "overrides", f"v2_{vid}.json")))
    rows = []
    for o, sfx in ORDERINGS:
        for name, cfg in (("V1", {}), (vid, ov)):
            rows.append(summarise("HOLDOUT-P", name, o, run_one("HOLDOUT-P", name, cfg, sfx)))
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(HERE, "results", "v2", "holdout.csv"), index=False)
    print(df[["variant", "ordering", "net", "max_dd", "trades", "decisions", "win_rate", "profit_factor"]]
          .to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    g = lambda v, o, c: float(df[(df.variant == v) & (df.ordering == o)].iloc[0][c])
    passed = all(g(vid, o, "net") > g("V1", o, "net") and g(vid, o, "max_dd") <= g("V1", o, "max_dd")
                 for o, _ in ORDERINGS)
    print(f"\nHOLDOUT-P verdict for {vid}: {'PASS' if passed else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
