#!/usr/bin/env python3
"""Experiment driver for the WhisperX adaptive fault-tolerant framework.

Single-instance guarded via a lockfile in --out. Refuses to start if
another driver is already running against the same output directory.
"""
import argparse, csv, json, os, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
PROJECT_ROOT = Path(CFG["project_root"])

STATUS_FIELDS = ["run_id", "condition_id", "run_index", "exit_code",
                 "wall_clock_s", "started_utc", "completed_utc"]


# ------------------------------------------------------------------ lock -----

def _is_process_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' check.

    Windows: os.kill(pid, 0) fails with WinError 87 because signal 0
    is not a valid Windows signal. Use tasklist instead.
    POSIX: os.kill(pid, 0) is the canonical check.
    """
    if sys.platform == "win32":
        import subprocess as _sp
        try:
            out = _sp.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=_sp.DEVNULL, text=True, timeout=5,
            )
            return str(pid) in out
        except (_sp.SubprocessError, OSError):
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def acquire_lock(out_root: Path) -> Path:
    """Single-instance guard. Exits if another live driver holds the lock.

    Two-phase:
      1. If a lock exists and its PID is alive, abort.
      2. Atomically create the lock with O_CREAT|O_EXCL to close the
         launch race between two drivers started in the same second.
    """
    lock = out_root / ".driver.lock"

    if lock.exists():
        try:
            other_pid = int(lock.read_text().strip())
        except (OSError, ValueError):
            print(f"WARN: corrupt lockfile at {lock}; removing.", file=sys.stderr)
            lock.unlink(missing_ok=True)
            other_pid = None

        if other_pid is not None:
            if _is_process_alive(other_pid):
                print(f"ABORT: another driver (PID {other_pid}) already holds {lock}",
                      file=sys.stderr)
                print("  If that process is dead, delete the lock and retry:",
                      file=sys.stderr)
                print(f'    Remove-Item "{lock}"', file=sys.stderr)
                sys.exit(3)
            print(f"Stale lockfile from dead PID {other_pid}; taking over.",
                  file=sys.stderr)
            lock.unlink(missing_ok=True)

    # Atomic create: O_CREAT|O_EXCL either succeeds or raises FileExistsError.
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
    except FileExistsError:
        try:
            other_pid = lock.read_text().strip()
        except OSError:
            other_pid = "unknown"
        print(f"ABORT: another driver (PID {other_pid}) already holds {lock}",
              file=sys.stderr)
        sys.exit(3)

    return lock


def release_lock(lock: Path) -> None:
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------- command -----

def build_command(cond: dict, run_id: str, config_json: Path) -> list:
    jar     = PROJECT_ROOT / "dist" / "AburimeSoundManager.jar"
    distlib = PROJECT_ROOT / "dist" / "lib"
    projlib = PROJECT_ROOT / "lib"
    fxmod   = PROJECT_ROOT / CFG["fx_module"]
    cp      = f"{jar};{distlib}\\*;{projlib}\\*"
    return [
        CFG["java_exe"],
        CFG["heap"],
        "-Dprocessrunner.diagnostics=true",
        f"-Daudiomanager.run_id={run_id}",
        "--module-path", str(fxmod),
        "--add-modules",
        "javafx.controls,javafx.fxml,javafx.graphics,"
        "javafx.base,javafx.media,javafx.swing,javafx.web",
        "-cp", cp,
        "audiomanager.Studio",
        "--headless",
        "--config", str(config_json),
        "--ablation",
        f"checkpoint={cond['checkpoint']},"
        f"retry={cond['retry']},"
        f"adaptive={cond['adaptive']}",
    ]


def write_run_config(cond: dict, run_dir: Path) -> Path:
    cfg = dict(CFG["base_config"])
    cfg["inputFiles"]        = cond["input_files"].split("|")
    cfg["maxParallel"]       = int(cond["batch_size"])
    cfg["skipSegmentation"]  = cond["skip_segmentation"] == "true"
    cfg["checkpointEnabled"] = cond["checkpoint"] == "true"
    cfg["retryEnabled"]      = cond["retry"]      == "true"
    cfg["adaptiveEnabled"]   = cond["adaptive"]   == "true"
    cfg["outputDirectory"]   = CFG["output_dir"]
    out = run_dir / "run_config.json"
    out.write_text(json.dumps(cfg, indent=2))
    return out


def move_results(run_id: str, run_dir: Path) -> bool:
    src = PROJECT_ROOT / "results" / run_id
    if not src.exists():
        return False
    dst = run_dir / "artefacts"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.move(str(src), str(dst))
    return True


def copy_log_tail(run_dir: Path, lines: int = 2000) -> None:
    log = Path(CFG["log_path"])
    if not log.exists():
        return
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    (run_dir / "log_tail.txt").write_text(chr(10).join(tail), encoding="utf-8")


def snapshot_estimator(run_dir: Path) -> None:
    src = Path.home() / ".audiomanager" / "time_estimates.json"
    if src.exists():
        shutil.copy2(src, run_dir / "time_estimates_after.json")


def append_status(out_root: Path, row: dict) -> None:
    """Append one row to run_status.csv, writing a header if the file is new."""
    status_path = out_root / "run_status.csv"
    new_file = not status_path.exists()
    with open(status_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STATUS_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


# ------------------------------------------------------------------ main -----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-condition", default=None)
    ap.add_argument("--only-run", type=int, default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--force-restart", action="store_true",
                    help="ignore an existing lockfile (dangerous)")
    args = ap.parse_args()

    manifest = list(csv.DictReader(open(args.manifest)))
    if args.only_condition:
        manifest = [c for c in manifest if c["condition_id"] == args.only_condition]
        if not manifest:
            print(f"No condition matches {args.only_condition}", file=sys.stderr)
            sys.exit(2)

    out_root = Path(args.out); out_root.mkdir(parents=True, exist_ok=True)

    if args.force_restart:
        (out_root / ".driver.lock").unlink(missing_ok=True)
    lock = acquire_lock(out_root) if not args.dry_run else None

    try:
        total_planned = sum(int(c["n_runs"]) for c in manifest)
        if args.only_run:
            total_planned = len(manifest)
        done = 0

        for cond in manifest:
            cond_id = cond["condition_id"]
            cond_dir = out_root / cond_id
            cond_dir.mkdir(parents=True, exist_ok=True)
            run_indices = ([args.only_run] if args.only_run
                           else range(1, int(cond["n_runs"]) + 1))

            for r in run_indices:
                run_id = f"{cond_id}_run{r:02d}"
                run_dir = cond_dir / f"run_{r:02d}"
                run_dir.mkdir(parents=True, exist_ok=True)

                if (run_dir / "artefacts" / "run_exit.json").exists():
                    print(f"[skip] {run_id} already complete")
                    continue

                cfg_path = write_run_config(cond, run_dir)
                cmd = build_command(cond, run_id, cfg_path)
                done += 1
                print(f"[{done}/{total_planned}] {run_id}")
                print(f"    config : {cfg_path}")

                if args.dry_run:
                    print("    (dry-run; not executing)")
                    continue

                t0 = time.time()
                try:
                    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
                    exit_code = proc.returncode
                except KeyboardInterrupt:
                    print("\nInterrupted by user. Aborting.")
                    return
                elapsed = time.time() - t0

                move_results(run_id, run_dir)
                copy_log_tail(run_dir)
                snapshot_estimator(run_dir)

                row = {
                    "run_id":        run_id,
                    "condition_id":  cond_id,
                    "run_index":     r,
                    "exit_code":     exit_code,
                    "wall_clock_s":  round(elapsed, 1),
                    "started_utc":   datetime.fromtimestamp(t0, timezone.utc).isoformat(),
                    "completed_utc": datetime.now(timezone.utc).isoformat(),
                }
                append_status(out_root, row)
                print(f"    exit={exit_code}  elapsed={elapsed:.1f}s")

        print("\nDriver finished.")
    finally:
        if lock is not None:
            release_lock(lock)


if __name__ == "__main__":
    main()
