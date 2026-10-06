#!/usr/bin/env python3
"""
Build the experimental corpus for the four-tier duration sweep.

Reads experiments/config.json. Produces, under config.corpus_root:
  normalised_5min/   - all 15 pool parts, loudnorm to -16 LUFS, 16 kHz mono
  1min/              - five 60-second clips sliced from the first five parts
  5min/              - five normalised parts, copied verbatim
  10min/             - five pairs concatenated
  15min/             - five triples concatenated
  corpus_manifest.csv - source-to-output mapping

Idempotent: skips outputs whose size already matches, unless --force.
"""
import argparse, csv, json, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))

SRC_DIR    = Path(CFG["corpus"]["source_dir"])
OUT_ROOT   = Path(CFG["corpus"]["root"])
FFMPEG     = CFG["corpus"]["ffmpeg"]
POOL       = CFG["corpus"]["pool"]
LOUDNORM   = "loudnorm=I=-16:TP=-1.5:LRA=11"
SAMPLE_RATE = "16000"


def run(cmd: list, desc: str) -> int:
    print(f"  $ {desc}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"    stderr: {proc.stderr[-500:]}")
    return proc.returncode


def probe_duration(path: Path) -> float:
    ffprobe = Path(FFMPEG).with_name("ffprobe.exe")
    if not ffprobe.exists():
        return -1.0
    r = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return -1.0


def up_to_date(dst: Path, min_bytes: int = 1000) -> bool:
    return dst.exists() and dst.stat().st_size >= min_bytes


# ---------------------------------------------------------------- stage 1 ----

def normalise_pool(force: bool) -> dict:
    """Produce normalised_5min/part_NNN.wav for each pool part."""
    out = OUT_ROOT / "normalised_5min"
    out.mkdir(parents=True, exist_ok=True)
    produced = {}
    print("\n=== Stage 1: normalise pool parts ===")
    for part in POOL:
        src = SRC_DIR / f"recording_20260908_174724_part_{part}.wav"
        dst = out / f"part_{part}_norm.wav"
        if not src.exists():
            print(f"  MISSING source: {src}")
            continue
        if not force and up_to_date(dst, 1_000_000):
            print(f"  skip (exists): {dst.name}")
        else:
            rc = run([FFMPEG, "-y", "-i", str(src),
                      "-af", LOUDNORM, "-ar", SAMPLE_RATE, "-ac", "1",
                      str(dst)],
                     f"loudnorm part_{part}")
            if rc != 0:
                print(f"    FAILED part_{part}")
                continue
        produced[part] = dst
    return produced


# ---------------------------------------------------------------- stage 2 ----

def make_1min(force: bool) -> list:
    """Slice the first 60s from the first five pool parts."""
    out = OUT_ROOT / "1min"
    out.mkdir(parents=True, exist_ok=True)
    produced = []
    print("\n=== Stage 2: 1-minute clips ===")
    for i, part in enumerate(POOL[:5], start=1):
        src = SRC_DIR / f"recording_20260908_174724_part_{part}.wav"
        dst = out / f"corpus_1min_{i:02d}.wav"
        if not force and up_to_date(dst, 100_000):
            print(f"  skip (exists): {dst.name}")
        else:
            rc = run([FFMPEG, "-y", "-i", str(src), "-t", "60",
                      "-af", LOUDNORM, "-ar", SAMPLE_RATE, "-ac", "1",
                      str(dst)],
                     f"1min from part_{part}")
            if rc != 0:
                print(f"    FAILED 1min clip {i}")
                continue
        produced.append((dst, [part]))
    return produced


# ---------------------------------------------------------------- stage 3 ----

def make_5min(normalised: dict, force: bool) -> list:
    """Copy the first five normalised parts as the 5-min tier."""
    out = OUT_ROOT / "5min"
    out.mkdir(parents=True, exist_ok=True)
    produced = []
    print("\n=== Stage 3: 5-minute clips ===")
    for i, part in enumerate(POOL[:5], start=1):
        src = normalised.get(part)
        if src is None or not src.exists():
            print(f"  missing normalised part_{part}")
            continue
        dst = out / f"corpus_5min_{i:02d}.wav"
        if not force and up_to_date(dst, 1_000_000):
            print(f"  skip (exists): {dst.name}")
        else:
            rc = run([FFMPEG, "-y", "-i", str(src), "-c", "copy", str(dst)],
                     f"copy 5min {dst.name}")
            if rc != 0:
                print(f"    FAILED 5min clip {i}")
                continue
        produced.append((dst, [part]))
    return produced


# ---------------------------------------------------------------- stage 4 ----

def concat_parts(parts: list, dst: Path, listfile: Path) -> int:
    lines = []
    for p in parts:
        src = SRC_DIR / f"recording_20260908_174724_part_{p}.wav"
        # concat demuxer needs forward-slash or escaped paths on Windows
        lines.append(f"file '{src.as_posix()}'")
    listfile.write_text("\n".join(lines), encoding="ascii")
    return run([FFMPEG, "-y", "-f", "concat", "-safe", "0",
                "-i", str(listfile), "-c", "copy", str(dst)],
               f"concat {len(parts)} parts -> {dst.name}")


def make_10min(force: bool) -> list:
    out = OUT_ROOT / "10min"
    out.mkdir(parents=True, exist_ok=True)
    # (pair index in POOL): 0+1, 2+3, 4+5, 6+7, 8+9
    pairs = [
        (POOL[0], POOL[1]),
        (POOL[2], POOL[3]),
        (POOL[4], POOL[5]),
        (POOL[6], POOL[7]),
        (POOL[8], POOL[9]),
    ]
    produced = []
    print("\n=== Stage 4: 10-minute clips ===")
    for i, pair in enumerate(pairs, start=1):
        dst = out / f"corpus_10min_{i:02d}.wav"
        if not force and up_to_date(dst, 2_000_000):
            print(f"  skip (exists): {dst.name}")
        else:
            lst = out / f"_concat_10min_{i:02d}.txt"
            rc = concat_parts(list(pair), dst, lst)
            if rc != 0:
                print(f"    FAILED 10min clip {i}")
                continue
            # normalise the concatenated result
            tmp = dst.with_suffix(".tmp.wav")
            rc2 = run([FFMPEG, "-y", "-i", str(dst),
                       "-af", LOUDNORM, "-ar", SAMPLE_RATE, "-ac", "1",
                       str(tmp)],
                      f"normalise {dst.name}")
            if rc2 != 0:
                print(f"    FAILED normalise {dst.name}")
                continue
            tmp.replace(dst)
        produced.append((dst, list(pair)))
    return produced


# ---------------------------------------------------------------- stage 5 ----

def make_15min(force: bool) -> list:
    out = OUT_ROOT / "15min"
    out.mkdir(parents=True, exist_ok=True)
    # five triples covering all 15 pool parts exactly once
    triples = [
        (POOL[0],  POOL[1],  POOL[2]),
        (POOL[3],  POOL[4],  POOL[5]),
        (POOL[6],  POOL[7],  POOL[8]),
        (POOL[9],  POOL[10], POOL[11]),
        (POOL[12], POOL[13], POOL[14]),
    ]
    produced = []
    print("\n=== Stage 5: 15-minute clips ===")
    for i, triple in enumerate(triples, start=1):
        dst = out / f"corpus_15min_{i:02d}.wav"
        if not force and up_to_date(dst, 3_000_000):
            print(f"  skip (exists): {dst.name}")
        else:
            lst = out / f"_concat_15min_{i:02d}.txt"
            rc = concat_parts(list(triple), dst, lst)
            if rc != 0:
                print(f"    FAILED 15min clip {i}")
                continue
            tmp = dst.with_suffix(".tmp.wav")
            rc2 = run([FFMPEG, "-y", "-i", str(dst),
                       "-af", LOUDNORM, "-ar", SAMPLE_RATE, "-ac", "1",
                       str(tmp)],
                      f"normalise {dst.name}")
            if rc2 != 0:
                print(f"    FAILED normalise {dst.name}")
                continue
            tmp.replace(dst)
        produced.append((dst, list(triple)))
    return produced


# -------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if outputs exist")
    args = ap.parse_args()

    print(f"Source dir : {SRC_DIR}")
    print(f"Output root: {OUT_ROOT}")
    print(f"FFmpeg     : {FFMPEG}")
    print(f"Pool       : {', '.join(POOL)}")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    norm = normalise_pool(args.force)
    m1   = make_1min(args.force)
    m5   = make_5min(norm, args.force)
    m10  = make_10min(args.force)
    m15  = make_15min(args.force)

    print("\n=== Verification ===")
    print(f"{'file':<40} {'duration_s':>12} {'size_MB':>10}")
    print(f"{'-'*40} {'-'*12} {'-'*10}")
    rows = []
    for clip, parts in (m1 + m5 + m10 + m15):
        dur = probe_duration(clip)
        size_mb = clip.stat().st_size / (1024 * 1024)
        print(f"{clip.name:<40} {dur:>12.2f} {size_mb:>10.1f}")
        rows.append({
            "clip_path": str(clip),
            "tier": clip.parent.name,
            "parts": "|".join(parts),
            "duration_s": f"{dur:.2f}",
            "size_mb": f"{size_mb:.1f}",
        })

    manifest = OUT_ROOT / "corpus_manifest.csv"
    with open(manifest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {manifest} ({len(rows)} clips)")


if __name__ == "__main__":
    main()
