# Fault-Injection Validation Data

Companion data for §6.1.1 of the paper. Two fault-injection runs
demonstrate the recovery-time benefit of the framework's segment-level
checkpoint mechanism.

## Files

- `fault_injection_adaptive.json` — adaptive configuration
  (checkpoint enabled, retry enabled). Kill fired at segment 6 of 10;
  recovery re-processed 4 remaining segments.
- `fault_injection_naive.json` — naive configuration
  (checkpoint disabled, retry disabled). Kill fired 20 minutes into the
  run; recovery re-processed the entire clip from segment 0.

## Summary

| Configuration | Recovery time (s) | Run elapsed (s) | Success |
|---|---|---|---|
| Adaptive (checkpoint + retry) | **657** | 648 | yes |
| Naive (no checkpoint, no retry) | **1,605** | 1,595 | yes |

Ratio: **2.4×**. The checkpoint mechanism reduced post-fault recovery
time by **59%** on the evaluated configuration.

## Method

Both runs used the same source clip (`corpus_5min_01`), the same
hardware (GREG-PC), and the same model and software stack as the paper's
main sweep. The framework was hard-killed — Java process and all child
Python subprocesses terminated — and then restarted with the driver's
`--resume` flag. Recovery time is measured from the restart command to
the driver's exit.

Kill timing differs between the two runs by necessity:

- **Adaptive**: killed by the fault-injection harness at segment 6 of 10,
  detected via the checkpoint ledger.
- **Naive**: killed at a fixed 20-minute wall-clock offset, because the
  naive configuration writes no runtime progress ledger for the harness
  to poll.

This is the limitation named in §6.8. The comparison is therefore
"same wall-clock offset, both complete", not "same segment index, both
complete".

## Reproduction

The fault-injection harness scripts are archived alongside the paper's
analysis pipeline:

- `tools/faultinject.ps1` — the progress-driven harness (used for adaptive)
- `tools/naive_fi.ps1` — the wall-clock standalone harness (used for naive)

Reproducing both runs requires the private audio corpus and the
framework binary, neither of which is redistributed (see the paper's
Data Availability statement).

## Not part of the 207-run sweep

These two runs are deliberately excluded from the main dataset because
the fault-injection protocol terminates the framework mid-run, which is
inconsistent with the sweep's "one clean run per condition per replicate"
design. They are reported as independent evidence for the fault-tolerance
claim, not as additional replicates of the sweep.