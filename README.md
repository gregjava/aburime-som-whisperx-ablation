# AburimeSoundManager WhisperX Ablation Study — Reproduction Materials

Companion materials for the paper:

> An Adaptive Fault-Tolerant Framework for Long-Audio Speech Transcription Using WhisperX (2026). <DOI>

## Contents

- **`code/`** — Analysis and experiment scripts
- **`data/analysis_outputs/`** — CSVs the paper's tables and figures derive from
- **`data/figures/`** — Figures appearing in the paper
- **`data/KNOWN_ANOMALIES.txt`** — Deployment anomaly log referenced in §6.6
- **`docs/`** — Supplementary manifests

## Full Dataset

The full experimental sweep that produced every table and figure in this
repository is archived on Zenodo:

- **DOI:** [10.5281/zenodo.23175556](https://doi.org/10.5281/zenodo.23175556)
- **Size:** ~4 GB uncompressed, ~4 MB compressed

The archive contains 207 runs (168 on the primary i7-1065G7 host, 39 on the
secondary i5-4310U host), the full factorial design across 56 conditions,
and `KNOWN_ANOMALIES.txt` documenting the deployment issues encountered
during collection.

## Reproducing the paper's tables

1. Download the sweep archive from Zenodo and unpack to `experiments/core_sweep/`.
2. Install Python dependencies:
   ```
   pip install pandas numpy scipy matplotlib statsmodels
   ```
3. Run the analysis:
   ```
   python code/analysis.py --sweep experiments/core_sweep --out experiments/analysis
   ```
4. The output CSVs correspond to paper tables as follows:

   | Paper table | Output file |
   |---|---|
   | Table 5 (segment outcomes) | `segments.csv` (grouped by `status`) |
   | Table 5b (hallucination by clip) | `hallucination_by_clip.csv` |
   | Table 6 (mixed-effects model) | `mixed_effects_summary_greg.txt` |
   | Table 7 (adaptive vs. naive t-tests) | `ttest_adaptive_vs_naive.csv` |
   | Table 9 (cross-platform ratios) | `cross_platform.csv` |

## Environment

Analysis was performed with Python 3.11.9 and:
- pandas 3.0.6
- numpy 2.4.6
- scipy 1.17.1
- matplotlib 3.11.2
- statsmodels 0.15.0

## Audio corpus

The source audio is not redistributed because it contains identifiable third-party speech. It is available on request under a data-use agreement — see the paper's Data Availability statement.

## License

- Code: Apache-2.0 (see `LICENSE`)
- Data (`data/`): CC-BY-4.0

## Citation

See `CITATION.cff`.