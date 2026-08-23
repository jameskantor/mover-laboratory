# Tech Evaluations

This is where a candidate technology gets considered, tested, and judged **before** it's
adopted — a library, a tool, a protocol, an architecture pattern that might help this
project. It's organized by candidate, not by date, and entries can sit at "still
evaluating" indefinitely; that's fine, this isn't a backlog to clear.

**This is separate from `BUILD_LOG.md`.** Once something here is actually adopted and
integrated, the integration work (what changed, what broke, what got fixed) gets logged
in `BUILD_LOG.md` as usual — this file only covers the comparison/evaluation phase that
leads to that decision, not the build itself. A `BUILD_LOG.md` entry can link back here
for the reasoning; this file should link forward to that entry once one exists.

## Template for a new entry

```
## <Technology name>

**What it is:** one or two sentences.

**Why we're looking at it:** the problem it might solve, or the gap it might fill.

**Status:** evaluating / adopted / rejected — with a date.

**What we tested:** concrete, reproducible — commands run, benchmarks, versions.

**Verdict:** the actual decision and why. If rejected, say what would change that.
```

---

## GPU-accelerated dataframe prep (RAPIDS cuDF / cudf.pandas / Polars GPU engine)

**What it is:** NVIDIA RAPIDS' `cuDF` is a GPU dataframe library with a pandas-like API;
`cudf.pandas` is an accelerator mode that runs existing pandas code on GPU with automatic
CPU fallback for unsupported ops; Polars' GPU engine uses cuDF underneath for lazy query
execution. Source: [How Much of a Data Science Workflow Can Run on a GPU
Today?](https://towardsdatascience.com/how-much-of-a-data-science-workflow-can-run-on-a-gpu-today-part-1-accelerating-data-preparation/)
(Part 1, data prep) — benchmarks pandas-style ops (filter, groupby, sort, value_counts)
on ~9-10M NYC taxi rows, reports meaningful speedups on groupby/aggregation-heavy work
when data stays on-GPU, but calls out small-dataset overhead, CPU/GPU transfer cost,
silent CPU fallback masking non-acceleration, and VRAM limits as real caveats.

**Why we're looking at it:** the RTX 5090 laptop GPU is already available and currently
unused for this project (reserved for "the ML training phase," per
[[mover_iceberg_pipeline]]) — exploratory, not driven by a current bottleneck. DuckDB's
existing performance is already strong (full aggregations over the 1.44B-row
`flowsheets` table run in under 3 seconds off Parquet, no GPU), so this isn't a "we're
stuck" evaluation — it's "would this help once feature engineering gets more complex,
particularly for a sequence/time-series model architecture (still undecided) that would
need heavy resampling/windowing over raw flowsheet data."

**Status:** evaluating, started 2026-08-22.

**Scope decision:** this evaluation includes solving the still-unresolved "how does a
GPU-enabled container reach the warehouse data" architecture gap (see
[[mover_iceberg_pipeline]]) as a prerequisite, not a separate later task — cuDF can't
read Iceberg metadata directly, only plain Parquet files, so a GPU container needs (a)
GPU passthrough working at all in this Docker Desktop setup, and (b) a way to resolve
which Parquet files currently belong to a bronze table.

**Investigated first, resolved:** whether the warehouse could safely be read via a naive
"every Parquet file under the table's data folder" approach, given `ingest.py`'s
`purge_source_file()` does a delete-then-append for crash-safe retries (which could in
general leave old snapshot files on disk after a delete/overwrite). Checked directly via
`iceberg_snapshots()` — **every snapshot across every table checked (`patient_medications`
56, `flowsheets` 730, `patient_labs` 59, `patient_information` 1) is a pure `append`,
zero `delete`/`overwrite` operations anywhere.** The one real historical crash-and-retry
(`patient_medications`'s `"0 NULL"` type-coercion bug) happened before that chunk's
`append()` ever ran, so the retry's purge found nothing to delete. Conclusion: no stale/
superseded Parquet files exist in this warehouse today, so file-level access is safe —
but resolve files via PyIceberg's `table.scan().plan_files()` rather than a raw
filesystem glob anyway, since that's correct by construction if this ever changes
(deletes/updates enter the picture later) at no extra cost now.

**Sanity gate — already cleared, no need to re-test.** GPU passthrough on this Docker
Desktop setup is confirmed working: `pytorch-dev` and `sleepy_lumiere` (both prior
containers on this machine) were run with `--gpus all` against
`pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel` — the exact image already reserved for
this project's ML training phase — and successfully trained a CNN against the RTX 5090.
Container logs confirm `CUDA Version 12.8.0` detected correctly. This is the single
biggest risk item and it's already resolved; skip straight to building the RAPIDS test
image.

**Compatibility check (2026-08-22):** RAPIDS requires "CUDA 12 with Driver 525.60.13+"
for pip installs, and specifically the `-devel` CUDA flavor (for NVRTC) rather than
`base`/`runtime` — `pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel` already satisfies both
(it's a `-devel` image, CUDA 12.8 falls within the supported "CUDA 12" family). Verified
the actual driver directly: `docker run --gpus all pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv` →
`NVIDIA GeForce RTX 5090 Laptop GPU, 592.01, 24463 MiB` — driver comfortably clears the
minimum. **VRAM is 24GB.** The full `flowsheets` table (~4.9GB compressed on disk across
all 10 bronze tables combined, via Parquet/ZSTD dictionary encoding) would likely **not**
fit decoded into 24GB of cuDF's flat columnar memory — compressed-on-disk and
decoded-in-VRAM sizes aren't comparable, and this table's heavily dictionary/RLE-
compressible categoricals (`FLO_NAME` 148 distinct, `FLO_DISPLAY_NAME` 121, etc.) can
expand substantially once decoded. Not a blocker: `flowsheets` is already Iceberg-
partitioned by year, so a chunked (one-year-at-a-time) processing pattern is a natural
fit for any full-table gold-building pass — new engineering effort, but not new
architecture. The planned benchmark below (a per-`LOG_ID` slice, not the full table)
fits comfortably in 24GB regardless; Dask-cuDF is only a requirement for a
whole-history-at-once pass, not for this evaluation's test.

**Install + smoke test (2026-08-22):** `pip install --extra-index-url
https://pypi.nvidia.com cudf-cu12` into `pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel`.
`cudf` version `26.08.00` installed, basic `DataFrame.sum()` correct. pip's resolver
flagged real version conflicts — `cudf-cu12` pulls newer shared `nvidia-*-cu12`
libraries (cuBLAS, cuRAND, cuSOLVER, etc.) than `torch==2.7.0+cu128` pins exactly.
**Tested empirically rather than trusting the warning**: ran a real GPU matmul via
`torch` and a real groupby via `cudf` in the same process, in the same container —
**both work correctly** (`torch.cuda.is_available()` → `True`, correct matmul result;
correct cuDF groupby result). So despite the pip conflict warning, one combined image
is viable for these operations — lowers the "second image to maintain" cost I'd
flagged earlier. Caveat: this is two basic smoke tests, not exhaustive — a real adopt
decision should re-check this after the actual benchmark workload runs, not just these
toy operations.

**What we're going to test (not yet run):**
1. Build a minimal test image (extend the already-pulled, GPU-proven
   `pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel`, or a lighter RAPIDS base) with `cudf`
   installed, run with `--gpus all` and the `mover-warehouse` volume mounted.
2. Pick one real, representative operation — not a toy — e.g. per-`LOG_ID` summary
   stats (count/mean/min/max/last) for core vitals (`Resp`/`Pulse`/`SpO2`/`Temp`) from
   `flowsheets`, the kind of aggregation an actual feature-engineering pass would need.
3. Run it 3 ways against the same resolved Parquet files, compare wall-clock and peak
   memory: **(a)** DuckDB SQL (current baseline to beat), **(b)** native `cuDF`
   (`cudf.read_parquet` + cuDF groupby), **(c)** `cudf.pandas` accelerator mode running
   equivalent pandas code unmodified.
4. Record real numbers here, not impressions — verdict should cite the actual benchmark.

**Verdict: rejected, 2026-08-23.** Not on performance grounds — the benchmark that did
run (`--engine cudf`, single month and full year, both clean) was slower than DuckDB at
this data size, but that's a minor factor. The real reason: attempting this evaluation
triggered a serious hardware/driver instability (full-system BSODs, later a full
unrecoverable freeze) on this laptop's RTX 5090 — see `docs/BUILD_LOG.md`'s
"GPU BSOD investigation" entry for the full incident and mitigation history. Combined
with DuckDB already meeting the actual need (sub-3s full aggregations over the
1.44B-row `flowsheets` table, no GPU required), there's no justification for continuing
to pursue cuDF specifically. Not revisiting unless a concrete bottleneck DuckDB can't
meet actually materializes.

---

## GPU model training (PyTorch / XGBoost-GPU / cuML)

**What it is:** using the RTX 5090 for actual model fitting, not data preparation —
`pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel` for a sequence/deep-learning model,
`xgboost`/`lightgbm` CUDA builds for GPU-accelerated gradient boosting on tabular data,
or `cuML` for GPU-accelerated scikit-learn-compatible estimators. Split out as its own
entry from the cuDF data-prep evaluation above because it's judged on different criteria
(training throughput, iteration speed on hyperparameter search) even though it shares
the same infra prerequisite (GPU container ↔ warehouse data access).

**Why we're looking at it:** this is the more obviously load-bearing GPU use case,
independent of whatever the data-prep verdict turns out to be. Which specific tooling
matters depends on the still-undecided model shape:
- **Tabular** (one row per surgery): GPU-accelerated boosting (`gpu_hist`/`device=cuda`
  in XGBoost, LightGBM's CUDA build) or `cuML` — the win is in hyperparameter-search
  throughput (hundreds of fits), not any single model's training time, since the gold
  table itself would be small.
- **Sequence/time-series** (raw vitals over time — the shape "real-time intraoperative
  deterioration prediction," the favored ML direction per [[mover_iceberg_pipeline]],
  actually points toward, since trajectory over recent vitals is the natural signal): a
  GPU isn't optional here, it's the only practical way to train an RNN/Transformer/
  temporal-CNN at this data volume. This is what the pytorch image was originally pulled
  for.

**Status:** parked, 2026-08-23. Blocked on the same "model shape" decision as the cuDF
entry above — that decision determines which tooling to actually test first — and now
also on re-establishing GPU stability, separately from the (rejected) cuDF path.

**What we tested:** nothing beyond the shared sanity gate (see cuDF entry above — GPU
passthrough confirmed working at a basic level) plus incidental evidence from Docker
container history: this same GPU trained a CNN successfully via
`pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel` as recently as 2026-01-25 (clean exit),
7 months before the BSOD investigation below. No sustained real training load has been
tested since the instability was found.

**Verdict:** not yet reached, parked rather than actively pursued. Two things need to
happen before this resumes: (1) the tabular vs. sequence architecture decision — both
this entry and the cuDF one were gated on that more than on the tech itself; (2) a
dedicated, deliberately-scoped, supervised PyTorch sustained-load retest, since the
cuDF-triggered BSOD investigation (`docs/BUILD_LOG.md`, "GPU BSOD investigation" entry,
2026-08-22/23) never actually established whether real PyTorch training hits the same
`0x133 DPC_WATCHDOG_VIOLATION` bug cuDF did — the June 2026 clean training run predates
the instability and isn't proof it's still safe today. Don't assume either the cuDF
verdict or the mitigations tried during that investigation (driver downgrade, GPU
Working Mode → dGPU-only, etc.) carry over — the last mitigation attempted there made
the failure mode worse, not better, so treat this as an open, unresolved risk rather
than something the cuDF investigation already answered.
