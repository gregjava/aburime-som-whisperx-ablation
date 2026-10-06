#!/usr/bin/env python3
"""
Analysis for the WhisperX adaptive fault-tolerant four-tier duration sweep.

Reads the collection tree produced by driver.py:
  experiments/core_sweep/<condition_id>/run_NN/
    artefacts/run_metadata.json
    artefacts/run_exit.json
    artefacts/file_timeline.csv
    artefacts/telemetry_*.csv
    artefacts/batch_summary.txt
    run_config.json
  ~/.audiomanager/logs/segment_logs/<run_id>/*.jsonl

Produces under --out (default experiments/analysis/):
  runs.csv             one row per run, all run-level metrics
  segments.csv         one row per segment across all runs
  files.csv            one row per file across all runs
  summary_by_condition.csv
  summary_by_tier.csv
  ablation_factorial.csv
  rtf_by_duration.csv
  cross_platform.csv
  figures/*.png

RTF convention: RTF = elapsed_s / total_audio_s.
  RTF > 1 means slower than realtime (processing takes longer than the audio).
  RTF < 1 means faster than realtime.
  This matches the ASR convention used in WhisperX benchmarks.

Usage:
  py experiments/analysis.py
  py experiments/analysis.py --sweep experiments/core_sweep --out experiments/analysis
  py experiments/analysis.py --through-run 50   # analyse only first N runs
"""
import argparse, csv, json, os, re, sys, glob
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
USER_HOME = Path.home()


# ---------------------------------------------------------------- loaders ----

def load_run(run_dir: Path) -> dict:
    """Load a single run directory. Returns dict or None if incomplete."""
    meta_path  = run_dir / "artefacts" / "run_metadata.json"
    exit_path  = run_dir / "artefacts" / "run_exit.json"
    cfg_path   = run_dir / "run_config.json"
    if not (meta_path.exists() and exit_path.exists() and cfg_path.exists()):
        return None

    try:
        meta = json.loads(meta_path.read_text())
        ex   = json.loads(exit_path.read_text())
        cfg  = json.loads(cfg_path.read_text())
    except Exception as e:
        print(f"  WARN: could not parse {run_dir.name}: {e}")
        return None

    # PATCHED: derive condition and run_index from directory name.
    # Directory names are either:
    #   run_01, run_02, run_03            (GREG-PC native)
    #   run_shalom_01, run_shalom_02, ... (SHALOM merged)
    # The run_index is the trailing numeric segment.
    cond_id = run_dir.parent.name
    run_dir_name = run_dir.name
    if run_dir_name.startswith("run_shalom_"):
        run_index = int(run_dir_name.rsplit("_", 1)[1])
        source = "shalom"
    elif run_dir_name.startswith("run_"):
        run_index = int(run_dir_name.split("_", 1)[1])
        source = "greg"
    else:
        print(f"  WARN: unrecognised run dir name {run_dir_name}")
        return None
    # PATCHED: run_id must be unique across GREG-PC and SHALOM. Use the
    # directory name directly so run_shalom_01 and run_01 don't collide.
    run_id = f"{cond_id}__{run_dir_name}"

    # Parse condition_id: <concurrency>_<ablation>_n<batch>_t<dur>min
    m = re.match(r"(naive|adaptive)_(none|ckpt|retry|full)_n(\d+)_t(\d+)min", cond_id)
    if not m:
        print(f"  WARN: cannot parse condition_id {cond_id}")
        return None
    concurrency, ablation, n, dur = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))

    # PATCHED: RTF convention documented at module top.
    # RTF = elapsed_s / total_audio_s; >1 means slower than realtime.
    total_audio_s = meta.get("total_audio_duration_s", 0)
    elapsed_s     = ex.get("elapsed_s", 0)
    rtf = (elapsed_s / total_audio_s) if total_audio_s > 0 else None

    row = {
        "run_id":            run_id,
        "condition_id":      cond_id,
        "source":            source,   # PATCHED: greg or shalom
        "concurrency_mode":  concurrency,
        "ablation":          ablation,
        "checkpoint":        cfg.get("checkpointEnabled", None),
        "retry":             cfg.get("retryEnabled", None),
        "adaptive":          cfg.get("adaptiveEnabled", None),
        "skip_segmentation": cfg.get("skipSegmentation", None),
        "batch_size":        n,
        "clip_duration_min": dur,
        "run_index":         run_index,  # PATCHED
        "n_input_files":     len(cfg.get("inputFiles", [])),
        "total_audio_s":     total_audio_s,
        "elapsed_s":         elapsed_s,
        "rtf":               rtf,
        "success":           ex.get("success", False),
        "completed":         ex.get("completed", 0),
        "failed":            ex.get("failed", 0),
        "total":             ex.get("total", 0),
        "cancelled":         ex.get("cancelled", False),
        "start_epoch_ms":    ex.get("start_epoch_ms"),
        "end_epoch_ms":      ex.get("end_epoch_ms"),
        "hostname":          meta.get("hostname"),
        "os_name":           meta.get("os_name"),       # PATCHED
        "max_heap_mb":       meta.get("max_heap_mb"),
        "available_procs":   meta.get("available_processors"),
        "gpu_available":     meta.get("gpu_available"),
    }

    # File timeline: pull per-file metrics
    timeline_path = run_dir / "artefacts" / "file_timeline.csv"
    if timeline_path.exists():
        try:
            tl = pd.read_csv(timeline_path)
            row["n_timeline_rows"] = len(tl)
            for col in ["transcription_wall_clock_ms", "total_pipeline_ms",
                        "queue_wait_ms", "preprocessing_ms", "model_acquisition_ms",
                        "output_saving_ms"]:
                if col in tl.columns:
                    row[f"sum_{col}"] = tl[col].sum()
                    row[f"max_{col}"] = tl[col].max()
        except Exception as e:
            print(f"  WARN timeline parse {run_dir.name}: {e}")

    # Segment log: aggregate per-run segment stats from the dedup'd view.
    seg_run_id = f"{cond_id}_run{run_index:02d}"
    seg_dir = USER_HOME / ".audiomanager" / "logs" / "segment_logs" / seg_run_id
    seg_stats = {"n_segments": 0, "seg_success": 0, "seg_empty": 0,
                 "seg_failed": 0, "seg_resumed": 0, "seg_resume_events": 0,
                 "seg_total_attempts": 0, "seg_hallucinations": 0,
                 "seg_total_words": 0}
    if seg_dir.exists():
        # Collect raw records first, then dedup, then aggregate.
        raw_records = []
        for jsonl in seg_dir.glob("*.jsonl"):
            try:
                for line in jsonl.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        raw_records.append(json.loads(line))
                    except Exception:
                        continue
            except Exception as e:
                print(f"  WARN segment log {jsonl.name}: {e}")
        if raw_records:
            tmp_df = deduplicate_segments(pd.DataFrame(raw_records))
            seg_stats["n_segments"] = len(tmp_df)
            seg_stats["seg_success"] = int((tmp_df["status"] == "success").sum())
            seg_stats["seg_empty"]   = int((tmp_df["status"] == "empty").sum())
            seg_stats["seg_failed"]  = int((tmp_df["status"] == "failed").sum())
            if "resume_event" in tmp_df.columns:
                seg_stats["seg_resumed"]       = int(tmp_df["status"].eq("resumed").sum())
                seg_stats["seg_resume_events"] = int(tmp_df["resume_event"].sum())
            seg_stats["seg_total_attempts"] = int(
                pd.to_numeric(tmp_df.get("attempt", 1), errors="coerce").fillna(1).sum()
            )
            if "hallucination_flag" in tmp_df.columns:
                seg_stats["seg_hallucinations"] = int(
                    tmp_df["hallucination_flag"].astype(bool).sum()
                )
            if "word_count" in tmp_df.columns:
                wc = pd.to_numeric(tmp_df["word_count"], errors="coerce").fillna(0)
                seg_stats["seg_total_words"] = int(wc[wc > 0].sum())
    row.update(seg_stats)
    row["segmentation_engaged"] = row["n_segments"] > 0

    return row


def load_all_runs(sweep_dir: Path, through_run: int = None) -> tuple:
    """Walk the sweep dir, load every run. Returns (runs_df, segments_df)."""
    runs = []
    segments = []

    cond_dirs = sorted([d for d in sweep_dir.iterdir() if d.is_dir()])
    for cond_dir in cond_dirs:
        run_dirs = sorted([d for d in cond_dir.iterdir()
                           if d.is_dir() and d.name.startswith("run_")])
        for run_dir in run_dirs:
            r = load_run(run_dir)
            if r is None:
                continue
            runs.append(r)

            # Segments: read the JSONL for this run to build a flat table
            seg_run_id = f"{r['condition_id']}_run{r['run_index']:02d}"
            seg_dir = USER_HOME / ".audiomanager" / "logs" / "segment_logs" / seg_run_id
            if seg_dir.exists():
                for jsonl in seg_dir.glob("*.jsonl"):
                    for line in jsonl.read_text().splitlines():
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        entry["run_id"]            = r["run_id"]         # PATCHED
                        entry["condition_id"]      = r["condition_id"]
                        entry["source"]            = r["source"]         # PATCHED
                        entry["concurrency_mode"]  = r["concurrency_mode"]
                        entry["ablation"]          = r["ablation"]
                        entry["batch_size"]        = r["batch_size"]
                        entry["clip_duration_min"] = r["clip_duration_min"]
                        segments.append(entry)

            if through_run and len(runs) >= through_run:
                return (
                    pd.DataFrame(runs),
                    deduplicate_segments(pd.DataFrame(segments)),
                )

    return (
        pd.DataFrame(runs),
        deduplicate_segments(pd.DataFrame(segments)),
    )


def deduplicate_segments(segments_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse duplicate segment records that arise when a run is re-executed.

    Dedup key is (run_id, source_clip, segment_id), NOT (run_id, file_id,
    segment_id): the app regenerates a fresh random 8-hex-char suffix on the
    preprocessed WAV filename for every attempt, so the same logical
    (file, segment) has a different file_id in each attempt's records.
    The stable identifier is the *source clip name* obtained by stripping the
    '_processed_<hex>.wav' suffix — matching the same normalization used by
    hallucination_by_clip() below.

    Rule applied per (run_id, source_clip, segment_id):
      1. Prefer real-outcome records (status in success/empty/failed)
         over 'resumed' sentinels.
      2. Among ties, keep the latest timestamp.
      3. Set a boolean 'resume_event' column: True if any duplicate row
         for this key had status='resumed'.
    """
    if segments_df.empty:
        return segments_df

    required = {"run_id", "file_id", "segment_id", "status", "timestamp"}
    if not required.issubset(segments_df.columns):
        return segments_df

    df = segments_df.copy()

    # Derive the stable source-clip identifier from file_id.
    df["__source_clip"] = (
        df["file_id"]
        .astype(str)
        .str.replace(r"_processed_[0-9a-f]+\.wav$", "", regex=True)
    )

    # Step 1: flag which (run_id, source_clip, segment_id) triples had a resume.
    has_resume = (
        df[df["status"] == "resumed"]
        .groupby(["run_id", "__source_clip", "segment_id"])
        .size()
        .rename("__had_resume")
    )
    df = df.merge(
        has_resume,
        on=["run_id", "__source_clip", "segment_id"],
        how="left",
    )
    df["__had_resume"] = df["__had_resume"].fillna(0).astype(int).astype(bool)

    # Step 2: preference tier — 0 for real outcomes, 1 for sentinels.
    df["__pref"] = df["status"].apply(
        lambda s: 0 if (isinstance(s, str) and s == "resumed") else 1
    )

    # Step 3: sort and dedup — keep highest-preference, latest-timestamp row.
    df = df.sort_values(
        ["run_id", "__source_clip", "segment_id", "__pref", "timestamp"]
    )
    df = df.drop_duplicates(
        subset=["run_id", "__source_clip", "segment_id"],
        keep="last",
    )

    # Step 4: rename helper column and drop internal helpers.
    df = df.rename(columns={"__had_resume": "resume_event"})
    df = df.drop(columns=["__pref", "__source_clip"])
    return df.reset_index(drop=True)


# --------------------------------------------------------------- analysis ----

def describe_by_condition(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["source", "condition_id", "concurrency_mode", "ablation",
                    "batch_size", "clip_duration_min", "hostname"]).agg(
        n_runs          =("run_id", "count"),
        n_success       =("success", "sum"),
        completion_rate =("success", "mean"),
        rtf_mean        =("rtf", "mean"),
        rtf_std         =("rtf", "std"),
        elapsed_mean_s  =("elapsed_s", "mean"),
        elapsed_std_s   =("elapsed_s", "std"),
        seg_total       =("n_segments", "sum"),
        seg_failed      =("seg_failed", "sum"),
        seg_resumed     =("seg_resumed", "sum"),
        hall_count      =("seg_hallucinations", "sum"),
    ).reset_index()
    # Rates computed after aggregation, from the aggregated counts.
    g["seg_fail_rate"] = g["seg_failed"] / g["seg_total"].replace(0, np.nan)
    g["hall_rate"]     = g["hall_count"] / g["seg_total"].replace(0, np.nan)
    return g


def describe_by_tier(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby(["source", "clip_duration_min", "batch_size", "concurrency_mode"]).agg(
        n_runs     =("run_id", "count"),
        rtf_mean   =("rtf", "mean"),
        rtf_std    =("rtf", "std"),
        rtf_median =("rtf", "median"),
    ).reset_index()


def ablation_factorial(df: pd.DataFrame) -> pd.DataFrame:
    """2^3 factorial on checkpoint x retry x concurrency, per source."""
    out = []
    for (src, ck, rt, cm), sub in df.groupby(
            ["source", "checkpoint", "retry", "concurrency_mode"]):
        out.append({
            "source":           src,
            "checkpoint":       ck,
            "retry":            rt,
            "concurrency_mode": cm,
            "n_runs":           len(sub),
            "completion_rate":  sub["success"].mean(),
            "rtf_mean":         sub["rtf"].mean(),
            "rtf_std":          sub["rtf"].std(),
            "seg_fail_rate":    sub["seg_failed"].sum() / max(sub["n_segments"].sum(), 1),
            "seg_resume_rate":  sub["seg_resumed"].sum() / max(sub["n_segments"].sum(), 1),
        })
    return pd.DataFrame(out)


def ttest_adaptive_vs_naive(df: pd.DataFrame) -> pd.DataFrame:
    """Welch's t-test of RTF, adaptive vs naive, at each (source, duration, n)."""
    rows = []
    for (src, dur, n), sub in df.groupby(["source", "clip_duration_min", "batch_size"]):
        a = sub[sub["concurrency_mode"] == "adaptive"]["rtf"].dropna()
        b = sub[sub["concurrency_mode"] == "naive"]["rtf"].dropna()
        if len(a) < 2 or len(b) < 2:
            continue
        t, p = stats.ttest_ind(a, b, equal_var=False)
        rows.append({
            "source":            src,
            "clip_duration_min": dur,
            "batch_size":        n,
            "n_adaptive":        len(a),
            "n_naive":           len(b),
            "adaptive_mean_rtf": a.mean(),
            "naive_mean_rtf":    b.mean(),
            "delta_rtf":         a.mean() - b.mean(),
            "pct_speedup":       100.0 * (b.mean() - a.mean()) / b.mean() if b.mean() else None,
            "t_stat":            t,
            "p_value":           p,
        })
    return pd.DataFrame(rows)


def cross_platform(df: pd.DataFrame) -> pd.DataFrame:
    """PATCHED: compare GREG-PC and SHALOM on overlapping conditions."""
    greg = df[df["source"] == "greg"]
    shal = df[df["source"] == "shalom"]
    if greg.empty or shal.empty:
        return pd.DataFrame()
    common = sorted(set(greg["condition_id"]) & set(shal["condition_id"]))
    rows = []
    for cond in common:
        g = greg[greg["condition_id"] == cond]["elapsed_s"].dropna()
        s = shal[shal["condition_id"] == cond]["elapsed_s"].dropna()
        if len(g) < 2 or len(s) < 2:
            continue
        rows.append({
            "condition_id":       cond,
            "greg_mean_s":        g.mean(),
            "greg_std_s":         g.std(),
            "shalom_mean_s":      s.mean(),
            "shalom_std_s":       s.std(),
            "n_greg":             len(g),
            "n_shalom":           len(s),
            "ratio_shalom_greg":  s.mean() / g.mean(),
            "pct_slower":         100.0 * (s.mean() - g.mean()) / g.mean(),
        })
    return pd.DataFrame(rows)


def hallucination_by_clip(segments_df: pd.DataFrame, out_dir: Path) -> pd.DataFrame:
    """PATCHED: summarize hallucination flags by source clip and segment index.

    Produces a per-clip table showing which source clips flagged, at which
    segment index, and with what signature (word rate, repetition ratio).
    The source clip name is recovered by stripping the '_processed_<hex>.wav'
    suffix that the app appends to each per-run preprocessed WAV.
    """
    if segments_df.empty:
        return pd.DataFrame()

    flagged = segments_df[segments_df["hallucination_flag"].astype(bool)].copy()
    if flagged.empty:
        empty = pd.DataFrame(columns=[
            "source_clip", "segment_index", "start_s", "end_s",
            "n_flags", "mean_word_count", "mean_word_rate",
            "mean_repetition_ratio", "trigger",
        ])
        empty.to_csv(out_dir / "hallucination_by_clip.csv", index=False)
        return empty

    # Recover the underlying source clip name.
    flagged["source_clip"] = (
        flagged["file_id"]
        .str.replace(r"_processed_[0-9a-f]+\.wav$", "", regex=True)
    )

    # Force numeric types for the aggregate columns.
    for col in ("segment_id", "start_s", "end_s", "word_count",
                "ngram_repetition_ratio", "duration_s"):
        if col in flagged.columns:
            flagged[col] = pd.to_numeric(flagged[col], errors="coerce")

    # Compute per-segment word rate; guard against zero duration.
    flagged["word_rate"] = (
        flagged["word_count"] /
        flagged["duration_s"].replace(0, np.nan)
    )

    # Determine which heuristic triggered the flag. The framework uses OR,
    # so a segment can satisfy both. We report the primary trigger for the
    # paper's mechanism discussion.
    def _trigger(row):
        rep = row.get("ngram_repetition_ratio", 0) or 0
        wr  = row.get("word_rate", 0) or 0
        rep_hit = rep > 0.5
        rate_hit = wr > 3.5
        if rep_hit and rate_hit: return "both"
        if rep_hit:              return "repetition"
        if rate_hit:             return "word_rate"
        return "unknown"

    flagged["trigger"] = flagged.apply(_trigger, axis=1)

    # Aggregate by (source_clip, segment_id) — one row per hallucination event.
    grouped = (
        flagged.groupby(["source_clip", "segment_id"])
        .agg(
            start_s            =("start_s", "first"),
            end_s              =("end_s", "first"),
            n_flags            =("hallucination_flag", "size"),
            mean_word_count    =("word_count", "mean"),
            mean_word_rate     =("word_rate", "mean"),
            mean_repetition_ratio=("ngram_repetition_ratio", "mean"),
            triggers_seen      =("trigger", lambda s: sorted(set(s))),
        )
        .reset_index()
        .rename(columns={"segment_id": "segment_index"})
        .sort_values(["source_clip", "segment_index"])
    )

    # Collapse the triggers_seen set into a single string for CSV output.
    grouped["trigger"] = grouped["triggers_seen"].apply(lambda xs: "+".join(xs))
    grouped = grouped.drop(columns=["triggers_seen"])

    # Round numeric columns for readability.
    for col in ("mean_word_count", "mean_word_rate", "mean_repetition_ratio"):
        grouped[col] = grouped[col].round(4)

    grouped.to_csv(out_dir / "hallucination_by_clip.csv", index=False)

    # Also emit a small summary CSV with per-duration-tier counts.
    flagged["duration_tier"] = (
        flagged["clip_duration_min"].astype(int).astype(str) + "min"
    )
    tier_summary = (
        flagged.groupby("duration_tier")
        .agg(
            n_flags       =("hallucination_flag", "size"),
            n_source_clips=("source_clip", "nunique"),
        )
        .reset_index()
    )
    tier_summary.to_csv(out_dir / "hallucination_by_tier.csv", index=False)

    # Print a one-line summary for the console.
    print(f"  Hallucination flags: {len(flagged)} segments "
          f"across {grouped['source_clip'].nunique()} source clips "
          f"({len(grouped)} unique clip+segment events)")

    return grouped


def mixed_effects(df: pd.DataFrame, source: str = "greg") -> str:
    """PATCHED: fit on one source at a time, with condition_id as group."""
    try:
        import statsmodels.formula.api as smf
    except ImportError:
        return "statsmodels not installed - skipping mixed-effects model"

    d = df[(df["source"] == source) & df["rtf"].notna()].copy()
    if d.empty:
        return f"No {source} data for mixed-effects model."
    d["concurrency_mode"] = d["concurrency_mode"].astype("category")
    d["checkpoint"]       = d["checkpoint"].astype("category")
    d["retry"]            = d["retry"].astype("category")
    d["condition_id"]     = d["condition_id"].astype("category")

    formula = ("rtf ~ C(concurrency_mode) + C(checkpoint) + C(retry) "
               "+ batch_size + clip_duration_min "
               "+ C(concurrency_mode):C(checkpoint) "
               "+ C(concurrency_mode):batch_size "
               "+ clip_duration_min:batch_size")

    try:
        model = smf.mixedlm(formula, data=d, groups=d["condition_id"]).fit(reml=False)
        return model.summary().as_text()
    except Exception as e:
        # Fall back to OLS with condition fixed effects if MixedLM fails
        try:
            formula_fe = formula + " + C(condition_id)"
            model = smf.ols(formula_fe, data=d).fit()
            return ("MixedLM failed, fell back to OLS with condition fixed effects.\n"
                    f"MixedLM error: {e}\n\n" + model.summary().as_text())
        except Exception as e2:
            return f"Both MixedLM and OLS-with-FE failed: {e} | {e2}"


# ---------------------------------------------------------------- figures ----

def fig_rtf_by_duration(df: pd.DataFrame, out_dir: Path, source: str = "greg"):
    sub_all = df[df["source"] == source]
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax, n in zip(axes, [1, 5]):
        sub = sub_all[sub_all["batch_size"] == n]
        if sub.empty:
            ax.set_title(f"n={n} (no data)")
            continue
        for cm, colour in [("naive", "tab:blue"), ("adaptive", "tab:orange")]:
            s2 = sub[sub["concurrency_mode"] == cm]
            if s2.empty:
                continue
            stats_g = s2.groupby("clip_duration_min")["rtf"].agg(["mean", "std"]).reset_index()
            ax.errorbar(stats_g["clip_duration_min"], stats_g["mean"],
                        yerr=stats_g["std"], label=cm, marker="o",
                        color=colour, capsize=4)
        ax.set_xlabel("clip duration (min)")
        ax.set_ylabel("RTF (elapsed / audio; >1 = slower than realtime)")
        ax.set_title(f"n={n}")
        ax.legend()
        ax.grid(alpha=0.3)
    fig.suptitle(f"RTF vs duration, by concurrency mode ({source})")
    fig.tight_layout()
    fig.savefig(out_dir / f"rtf_by_duration_{source}.png", dpi=150)
    plt.close(fig)


def fig_completion_heatmap(df: pd.DataFrame, out_dir: Path, source: str = "greg"):
    sub_all = df[df["source"] == source]
    pivot = sub_all.groupby(["clip_duration_min", "batch_size", "ablation",
                             "concurrency_mode"])["success"].mean().reset_index()
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for ax, cm in zip(axes, ["naive", "adaptive"]):
        sub = pivot[pivot["concurrency_mode"] == cm]
        if sub.empty:
            ax.set_title(f"{cm} (no data)")
            continue
        table = sub.pivot_table(index="ablation",
                                columns=["clip_duration_min", "batch_size"],
                                values="success")
        im = ax.imshow(table, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
        ax.set_xticks(range(len(table.columns)))
        ax.set_xticklabels([f"{d}m n={n}" for d, n in table.columns], rotation=45)
        ax.set_yticks(range(len(table.index)))
        ax.set_yticklabels(table.index)
        ax.set_title(f"{cm}")
        fig.colorbar(im, ax=ax, label="completion rate")
    fig.suptitle(f"Completion rate by ablation x duration x batch ({source})")
    fig.tight_layout()
    fig.savefig(out_dir / f"completion_heatmap_{source}.png", dpi=150)
    plt.close(fig)


def fig_ablation_factorial(df: pd.DataFrame, out_dir: Path, source: str = "greg"):
    fac = ablation_factorial(df)
    fac = fac[fac["source"] == source]
    if fac.empty:
        return
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for ax, metric in zip(axes, ["rtf_mean", "seg_fail_rate"]):
        for cm, colour in [("naive", "tab:blue"), ("adaptive", "tab:orange")]:
            sub = fac[fac["concurrency_mode"] == cm]
            if sub.empty:
                continue
            labels = [f"{ck}_{rt}" for ck, rt in zip(sub["checkpoint"], sub["retry"])]
            ax.bar(np.arange(len(sub)) + (0.35 if cm == "adaptive" else 0),
                   sub[metric], width=0.35, label=cm, color=colour)
        ax.set_xticks(np.arange(len(fac) // 2) + 0.175)
        ax.set_xticklabels([f"{ck}_{rt}" for ck, rt in
                            zip(fac["checkpoint"][::2], fac["retry"][::2])], rotation=45)
        ax.set_ylabel(metric)
        ax.set_title(metric)
        ax.legend()
        ax.grid(alpha=0.3, axis="y")
    fig.suptitle(f"Ablation factorial: RTF and segment-failure rate ({source})")
    fig.tight_layout()
    fig.savefig(out_dir / f"ablation_factorial_{source}.png", dpi=150)
    plt.close(fig)


def fig_segment_status(df: pd.DataFrame, out_dir: Path, source: str = "greg"):
    """Stacked bar: segment status composition per condition."""
    if df.empty or "status" not in df.columns:
        return
    sub = df[df["source"] == source]
    if sub.empty:
        return
    g = sub.groupby(["clip_duration_min", "batch_size", "concurrency_mode",
                     "status"]).size().unstack(fill_value=0)
    g = g.div(g.sum(axis=1), axis=0)
    fig, ax = plt.subplots(figsize=(12, 6))
    g.plot(kind="bar", stacked=True, ax=ax, colormap="tab20")
    ax.set_ylabel("fraction of segments")
    ax.set_title(f"Segment status composition by condition ({source})")
    ax.legend(title="status", bbox_to_anchor=(1.02, 1), loc="upper left")
    fig.tight_layout()
    fig.savefig(out_dir / f"segment_status_{source}.png", dpi=150)
    plt.close(fig)


def fig_cross_platform(xp: pd.DataFrame, out_dir: Path):
    """PATCHED: scatter of SHALOM vs GREG-PC elapsed times."""
    if xp.empty:
        return
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.scatter(xp["greg_mean_s"], xp["shalom_mean_s"], s=60, alpha=0.7)
    lim = max(xp["greg_mean_s"].max(), xp["shalom_mean_s"].max()) * 1.05
    ax.plot([0, lim], [0, lim], "k--", alpha=0.4, label="y = x")
    # Regression line
    m, b = np.polyfit(xp["greg_mean_s"], xp["shalom_mean_s"], 1)
    xs = np.array([0, lim])
    ax.plot(xs, m*xs + b, "r-", alpha=0.6, label=f"fit: y = {m:.2f}x + {b:.1f}")
    ax.set_xlabel("GREG-PC mean elapsed (s)")
    ax.set_ylabel("SHALOM mean elapsed (s)")
    ax.set_title("Cross-platform scaling on overlapping conditions")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "cross_platform_scaling.png", dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default=str(HERE / "core_sweep"))
    ap.add_argument("--out", default=str(HERE / "analysis"))
    ap.add_argument("--through-run", type=int, default=None)
    args = ap.parse_args()

    sweep_dir = Path(args.sweep)
    out_dir   = Path(args.out)
    fig_dir   = out_dir / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)

    print(f"Reading sweep: {sweep_dir}")
    runs_df, segments_df = load_all_runs(sweep_dir, args.through_run)
    print(f"  Loaded {len(runs_df)} runs, {len(segments_df)} segment records")

    if runs_df.empty:
        print("No complete runs found - exiting")
        return

    # PATCHED: sanity check on source split
    print(f"  Source split: greg={sum(runs_df['source']=='greg')}, "
          f"shalom={sum(runs_df['source']=='shalom')}")

    runs_df.to_csv(out_dir / "runs.csv", index=False)
    if not segments_df.empty:
        segments_df.to_csv(out_dir / "segments.csv", index=False)
    print(f"  Wrote runs.csv ({len(runs_df)} rows), segments.csv ({len(segments_df)} rows)")

    by_cond = describe_by_condition(runs_df)
    by_cond.to_csv(out_dir / "summary_by_condition.csv", index=False)
    print(f"  Wrote summary_by_condition.csv")

    by_tier = describe_by_tier(runs_df)
    by_tier.to_csv(out_dir / "summary_by_tier.csv", index=False)
    print(f"  Wrote summary_by_tier.csv")

    fac = ablation_factorial(runs_df)
    fac.to_csv(out_dir / "ablation_factorial.csv", index=False)
    print(f"  Wrote ablation_factorial.csv")

    ttests = ttest_adaptive_vs_naive(runs_df)
    ttests.to_csv(out_dir / "ttest_adaptive_vs_naive.csv", index=False)
    print(f"  Wrote ttest_adaptive_vs_naive.csv")

    # PATCHED: cross-platform comparison
    xp = cross_platform(runs_df)
    if not xp.empty:
        xp.to_csv(out_dir / "cross_platform.csv", index=False)
        print(f"  Wrote cross_platform.csv ({len(xp)} conditions)")
    
    # PATCHED: hallucination-by-clip aggregation
    halluc = hallucination_by_clip(segments_df, out_dir)

    print("\nFitting mixed-effects model on GREG-PC only...")
    mixed = mixed_effects(runs_df, source="greg")
    (out_dir / "mixed_effects_summary_greg.txt").write_text(mixed)
    print(mixed[:500])

    print("\nGenerating figures...")
    for src in ("greg", "shalom"):
        fig_rtf_by_duration(runs_df, fig_dir, source=src)
        fig_completion_heatmap(runs_df, fig_dir, source=src)
        fig_ablation_factorial(runs_df, fig_dir, source=src)
        fig_segment_status(segments_df, fig_dir, source=src)
    fig_cross_platform(xp, fig_dir)
    print(f"  Wrote figures to {fig_dir}")

    print("\nDone.")
    print(f"  Runs analysed    : {len(runs_df)}")
    print(f"  Success rate     : {runs_df['success'].mean():.3f}")
    print(f"  Median RTF       : {runs_df['rtf'].median():.3f}")
    print(f"  Median wall clock: {runs_df['elapsed_s'].median():.1f}s")


if __name__ == "__main__":
    main()