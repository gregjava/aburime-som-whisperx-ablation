#!/usr/bin/env python3
"""Build the 56-condition four-tier duration sweep manifest."""
import csv, json
from itertools import product
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG  = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))

CORPUS_ROOT = Path(CFG["corpus"]["root"])
N_RUNS      = 3

CONCURRENCY = ["naive", "adaptive"]
ABLATION    = [
    ("none",  "false", "false"),
    ("ckpt",  "true",  "false"),
    ("retry", "false", "true"),
    ("full",  "true",  "true"),
]


def clip_paths(duration_min):
    tier_dir = CORPUS_ROOT / (str(duration_min) + "min")
    return [str(tier_dir / ("corpus_" + str(duration_min) + "min_" + format(i, "02d") + ".wav"))
            for i in range(1, 6)]


def input_files_for(duration_min, batch_size):
    clips = clip_paths(duration_min)
    if batch_size == 1:
        return [clips[0]]
    return clips


def main():
    rows = []
    for (cmode, (alabel, ck, rt), bs, dur) in product(
            CONCURRENCY, ABLATION, [1, 5], [1, 5, 10, 15]):
        if dur == 15 and bs == 5:
            continue
        cond_id = cmode + "_" + alabel + "_n" + str(bs) + "_t" + str(dur) + "min"
        adaptive = "true" if cmode == "adaptive" else "false"
        # Segmentation is ALWAYS on. The naive/adaptive axis tests the
        # concurrency policy only -- not the segmentation policy. Earlier
        # revisions set skip_seg=true for naive, which silently disabled the
        # segmented pipeline and rendered the ablation flags (checkpoint,
        # retry) inert across the entire naive half of the design.
        skip_seg = "false"
        rows.append({
            "condition_id":      cond_id,
            "concurrency_mode":  cmode,
            "checkpoint":        ck,
            "retry":             rt,
            "adaptive":          adaptive,
            "skip_segmentation": skip_seg,
            "batch_size":        bs,
            "clip_duration_min": dur,
            "input_files":       "|".join(input_files_for(dur, bs)),
            "n_runs":            N_RUNS,
        })

    out = HERE / "manifest.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    total_runs = sum(int(r["n_runs"]) for r in rows)
    print("Wrote " + str(out))
    print("  " + str(len(rows)) + " conditions x " + str(N_RUNS) + " replicates = " + str(total_runs) + " runs")
    print()
    print("  tier       conditions     runs")
    for dur in [1, 5, 10, 15]:
        c = [r for r in rows if r["clip_duration_min"] == dur]
        print("  " + str(dur) + "min        " + str(len(c)).rjust(6) + "  " + str(len(c)*N_RUNS).rjust(6))


if __name__ == "__main__":
    main()
