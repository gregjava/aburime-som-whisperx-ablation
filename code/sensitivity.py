#!/usr/bin/env python3
"""
Sensitivity analysis for the ASoM sweep (GREG-PC, 168 runs).

Usage:  python sensitivity.py path/to/runs.csv [out.json]

Reproduces Table 6 exactly (scenario S0), then refits the SAME model under
alternative data-handling scenarios, and re-tests the headline cell
(n=5 x 1-minute clips, adaptive vs naive) with outlier-robust and
cluster-aware methods. Requires: pandas, numpy, scipy, statsmodels.
"""
import sys, json, warnings
import numpy as np, pandas as pd
import statsmodels.formula.api as smf
from scipy import stats
warnings.filterwarnings("ignore")

RUNS = sys.argv[1] if len(sys.argv) > 1 else "runs.csv"
OUT = sys.argv[2] if len(sys.argv) > 2 else "sensitivity.json"

# First-attempt elapsed times of the two runs that were re-executed after the
# analysis flagged them (KNOWN_ANOMALIES.txt, item 5). Edit if the log changes.
ORIGINALS = {
    ("adaptive_full_n1_t1min", 2): 1526.6,
    ("naive_full_n1_t15min", 1): 29740.8,
}

FORMULA = ("{y} ~ C(concurrency_mode)*C(checkpoint) + C(retry) "
           "+ batch_size*C(concurrency_mode) + clip_duration_min*batch_size")
KEYS = {
    "naive":     "C(concurrency_mode)[T.naive]",
    "checkpoint": "C(checkpoint)[T.True]",
    "retry":     "C(retry)[T.True]",
    "naive_x_ckpt": "C(concurrency_mode)[T.naive]:C(checkpoint)[T.True]",
}

df = pd.read_csv(RUNS)
g = df[df.source == "greg"].copy()
g["checkpoint"] = g["checkpoint"].astype(str)
g["retry"] = g["retry"].astype(str)
assert len(g) == 168, f"expected 168 GREG runs, found {len(g)}"


def mixed(d, y="rtf"):
    return smf.mixedlm(FORMULA.format(y=y), d, groups=d["condition_id"]).fit(reml=False)


def pack(m):
    out = {"scale": float(m.scale)}
    for k, name in KEYS.items():
        out[k] = {"coef": float(m.params[name]), "p": float(m.pvalues[name])}
    return out


results = {"mixed": {}}
results["mixed"]["S0 As reported"] = pack(mixed(g))

# S1: reinstate the two original (pre-re-execution) values
g1 = g.copy()
for (cond, idx), secs in ORIGINALS.items():
    mask = (g1.condition_id == cond) & (g1.run_index == idx)
    assert mask.sum() == 1, (cond, idx)
    g1.loc[mask, "rtf"] = secs / g1.loc[mask, "total_audio_s"].values[0]
results["mixed"]["S1 Originals reinstated"] = pack(mixed(g1))

# S2: log-transformed RTF
g2 = g.copy(); g2["lrtf"] = np.log(g2.rtf)
results["mixed"]["S2 log(RTF)"] = pack(mixed(g2, "lrtf"))

# S3: symmetric treatment - drop the two largest runs that were RETAINED
drop = g.sort_values("rtf", ascending=False).head(2)
results["dropped_in_S3"] = drop[["run_id", "elapsed_s", "rtf"]].round(3).to_dict("records")
results["mixed"]["S3 Two largest retained runs dropped"] = pack(mixed(g.drop(drop.index)))

# S4: condition medians (one robust value per condition), OLS on 56 points
cm = (g.groupby(["condition_id", "concurrency_mode", "checkpoint", "retry",
                 "batch_size", "clip_duration_min"]).rtf.median().reset_index())
m4 = smf.ols(FORMULA.format(y="rtf"), cm).fit()
s4 = {"scale": None}
for k, name in KEYS.items():
    s4[k] = {"coef": float(m4.params[name]), "p": float(m4.pvalues[name])}
results["mixed"]["S4 Condition medians (OLS, n=56)"] = s4

# ---- pairwise adaptive-vs-naive cells ------------------------------------
cells = []
for (d, b), sub in g.groupby(["clip_duration_min", "batch_size"]):
    a = sub[sub.concurrency_mode == "adaptive"]; n = sub[sub.concurrency_mode == "naive"]
    am = a.groupby(["checkpoint", "retry"]).rtf.mean(); nm = n.groupby(["checkpoint", "retry"]).rtf.mean()
    idx = am.index.intersection(nm.index)
    cells.append({
        "duration": int(d), "batch": int(b), "n_adaptive": len(a), "n_naive": len(n),
        "speedup_pct": float((n.rtf.mean() - a.rtf.mean()) / n.rtf.mean() * 100),
        "welch_p": float(stats.ttest_ind(a.rtf, n.rtf, equal_var=False).pvalue),
        "welch_log_p": float(stats.ttest_ind(np.log(a.rtf), np.log(n.rtf), equal_var=False).pvalue),
        "mwu_p": float(stats.mannwhitneyu(a.rtf, n.rtf).pvalue),
        "paired_by_ablation_p": float(stats.ttest_rel(am[idx], nm[idx]).pvalue),
        "n_pairs": int(len(idx)),
    })
# Holm correction across the 7 cells (Welch p)
ps = np.array([c["welch_p"] for c in cells]); order = np.argsort(ps); run = 0.0
holm = np.empty(len(ps))
for rank, i in enumerate(order):
    run = max(run, (len(ps) - rank) * ps[i]); holm[i] = min(1.0, run)
for c, h in zip(cells, holm):
    c["welch_holm_p"] = float(h)
results["cells"] = cells

json.dump(results, open(OUT, "w"), indent=2)

# ---- console summary ------------------------------------------------------
print(f"{'Scenario':42s} scale  naive(p)        ckpt(p)         retry(p)        naive x ckpt(p)")
for name, r in results["mixed"].items():
    sc = f"{r['scale']:5.2f}" if r["scale"] is not None else "  n/a"
    cols = "  ".join(f"{r[k]['coef']:+.2f} ({r[k]['p']:.3f})" for k in KEYS)
    print(f"{name:42s} {sc}  {cols}")
print("\nCell                 speedup   Welch   Holm    log-Welch  MWU    paired-by-ablation")
for c in cells:
    print(f"t{c['duration']:>2}min n{c['batch']}  {c['speedup_pct']:+7.1f}%  {c['welch_p']:.3f}  "
          f"{c['welch_holm_p']:.3f}  {c['welch_log_p']:.3f}     {c['mwu_p']:.3f}  "
          f"{c['paired_by_ablation_p']:.3f} (k={c['n_pairs']})")
