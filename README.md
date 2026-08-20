# Mover Laboratory

Building an Apache Iceberg data lake from the [MOVER](https://mover.ics.uci.edu/) (Medical
Informatics Operating Room Vitals and Events Repository) dataset — de-identified Epic EMR
data for surgical patients — to support ML models on perioperative outcomes.

Source paper: Samad et al., *"Medical Informatics Operating Room Vitals and Events
Repository (MOVER): a public-access operating room database"*, JAMIA Open 2023,
https://doi.org/10.1093/jamiaopen/ooad084

**This data is HIPAA-governed (de-identified, credentialed-access).** Never commit raw
data, samples, or query output containing patient-level rows to git or any third-party
service. `.gitignore` already excludes CSVs, the Iceberg warehouse, and logs.

**This README's job is to let someone else stand up their own copy of this lab** — their
own machine, their own credentialed MOVER download, the same containers and scripts. It
does not ship data (the MOVER agreement doesn't allow that), only the tooling to rebuild
it from your own access. For the story of how *we* built and are iterating on our own copy
— experiments, benchmarks, bugs found and fixed — see `docs/BUILD_LOG.md` instead.

## Status

**Bronze layer: done.** All 10 source tables are ingested into Iceberg and row-count-
verified against the source CSVs. 1,502,559,444 rows total. The warehouse lives in a
Docker-native volume (not a Windows-visible path) so it stays close to the compute that
reads it — see `docs/BUILD_LOG.md` for the benchmark that motivated this. Windows-side
access is via `jupyter.ps1`, which also serves the tables over the network (DuckDB's
Quack remote protocol) alongside JupyterLab — confirmed working from both the native
DuckDB CLI and DBeaver.

**Silver / gold: not started.** See `docs/DATA_DICTIONARY.md` → "Open items" for the specific
next steps (casing/unit fixes, then feature tables once an ML target is chosen).

## Data sources

| Location | What's there |
|---|---|
| `G:\MOVER\MOVER\` (external drive — only present when connected) | **Source of truth.** The full raw MOVER download as delivered: `EPIC_EMR.tar.gz`, `Epic_flowsheets_cleaned.tar.gz`, `EPIC_patient_measurments.tar.gz` (raw/uncleaned flowsheets — superseded, don't use), `epic_wave_1/2/3_v2.tar.gz` (~307GB, raw waveforms — out of scope so far), `sis_emr.tar.gz` (older SIS system, different schema, not linked to EPIC IDs), plus `MOVER.pdf` / `MOVER_Description/` (dataset docs). |
| `D:\Data_Science_Projects\Mover\EMR\EPIC_EMR\` | Working copy: the 9 EMR CSVs + `flowsheets_cleaned/` (19 parts), extracted from the G: tarballs so ingestion has fast, always-available local disk to read from. Redundant with G: once bronze is ingested — safe to delete to reclaim space (~153GB) if needed. |
| `mover-warehouse` (Docker-native volume, not a Windows path) | **The actual lake.** Bronze Iceberg tables (data + metadata), ~4.9GB (Parquet/ZSTD compressed). Lives in Docker's own storage for fast access from compute; reach it via `query.ps1` / `jupyter.ps1`, not a Windows file path. |

Column-level definitions, type-correction decisions, and the full ingestion log live in
`docs/DATA_DICTIONARY.md` — that file is the living reference for schema questions.

## Tools & containers

Native Windows Python doesn't work for this project — Windows Smart App Control blocks
pip-installed compiled binaries like pyarrow's DLLs, and that setting can't be safely
disabled. So everything runs in Docker, kept deliberately separate from any other
project's environment.

| Tool | Image / install | Purpose |
|---|---|---|
| **`mover-laboratory:latest`** | Built from project `Dockerfile` (`python:3.12-slim` + pyiceberg, duckdb, pandas, pyarrow, dask, jupyterlab) | One image, several roles: **ingestion** (`scripts/ingest.py`, resumable/idempotent), **EDA** (`query.ps1`), and **EDA + network serving together** (`jupyter.ps1`, which runs `scripts/start_lab.sh` — JupyterLab in the foreground plus the Quack server in the background, one container for both). Same dependency stack covers all of it. |
| **`pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel`** | Pulled, stock, **not yet customized** | Reserved for the ML training phase (GPU: RTX 5090 laptop). Has no pandas/pyarrow/duckdb yet, and how it will actually reach the warehouse data is still unresolved — needs either its own Dockerfile layer, a pre-exported training dataset, or something else entirely. Open question, not yet designed. |
| **DBeaver CE** | `winget install --id 9PNKDR50694P --source msstore` (Store build — trusted by Smart App Control) | Query the warehouse from native Windows with a GUI, over the network via the `quack-jdbc` driver (alpha) — DBeaver has no direct file access to the warehouse anymore, since it isn't on a Windows path. See `docs/BUILD_LOG.md` for setup (jar URL, driver class, connection string). |
| **DuckDB CLI** | `winget install --id DuckDB.cli` | Same warehouse, from a terminal, via `ATTACH '...' (TYPE quack)` or `quack_query(...)` — confirmed working. No Docker needed on this side. |

### Running things

The image's default command launches the lab (JupyterLab + Quack) automatically, so a
plain `docker run` with the right mounts (no command needed) already works — `jupyter.ps1`
just adds the port publishing and env vars as a one-line wrapper.

```powershell
# Interactive Python/DuckDB shell against all 10 bronze tables
.\query.ps1

# JupyterLab at localhost:8888 AND the bronze tables served over the network (Quack) at
# localhost:9494, in one container -- for DBeaver / other DuckDB clients on Windows, see
# docs/BUILD_LOG.md for the driver setup
.\jupyter.ps1

# Re-run ingestion (idempotent — safe to re-run, tracks progress in
# iceberg_warehouse/_ingestion_status.json)
docker run --rm -v "/d/Data_Science_Projects/Mover:/work" -v mover-warehouse:/work/iceberg_warehouse mover-laboratory:latest python /work/scripts/ingest.py
```

By default `scripts/catalog.py` targets a portable `/work/iceberg_warehouse` — nothing in
the code assumes Windows or a specific folder layout, so a fresh clone against your own
data needs none of the env vars below. On *this* warehouse specifically, `query.ps1` /
`jupyter.ps1` still pass `WINDOWS_HOST_PATH` / `MOVER_WAREHOUSE_DIR`, because the existing
manifests were baked with a Windows-drive-letter-shaped path back when the warehouse
lived on `D:\` — those env vars just make that old path resolve (via a symlink) through
to the current Docker volume. Cosmetic, not required for a new setup.

## Repo layout

```
Dockerfile, entrypoint.sh      → mover-laboratory image build
jupyter.ps1, query.ps1         → entry points into that image
scripts/
  catalog.py                   → PyIceberg SqlCatalog config (warehouse location)
  schemas.py                   → per-table Iceberg schemas + partition specs
  ingest.py                    → CSV → bronze Iceberg driver
  audit_schema.py              → schema/type audit against source CSVs
  shell.py                     → what query.ps1 runs (DuckDB views over bronze)
  start_lab.sh                 → what jupyter.ps1 runs (Jupyter + Quack server together)
  quack_server.py              → registers bronze views, serves them over Quack
  gen_dbeaver_sql.py           → regenerates scripts/dbeaver_setup.sql (legacy direct-file
                                  DBeaver setup, superseded by Quack — see docs/BUILD_LOG.md)
docs/
  DATA_DICTIONARY.md           → canonical column definitions + ingestion log
  BUILD_LOG.md                 → our own build/experiment history (not the repro guide)
EMR/                           → working CSV copy (gitignored)
```

The warehouse itself (`bronze/`, `catalog.db`, `_ingestion_status.json`) lives in the
`mover-warehouse` Docker volume, not a project subdirectory — `docker volume ls` / `docker
volume inspect mover-warehouse` to find it, not the filesystem.

## Open items

- How the pytorch/training container will actually reach the warehouse data — genuinely
  unresolved, not just undecided (see Tools table above).
- Quack itself is beta (stabilizes with DuckDB v2.0, Sept 2026) — treat as a prototype,
  not yet the documented pattern to recommend to others until it stabilizes.
- No experiment tracking (MLflow/W&B) set up yet — needed before the ML phase.
- Single copy of the data outside G: — no offsite/cloud backup.
- See `docs/DATA_DICTIONARY.md` → "Open items / TODO" for the data-pipeline-specific next
  steps (silver layer design, gold feature tables). Postop-complication label validation
  is done — the 11-class taxonomy is confirmed against the paper and ready to use.

## Development log

Everything above is what you need to stand this up yourself. This section isn't that —
it's a pointer to our own build process on this particular machine: experiments,
benchmarks, protocol research, and bugs hit and fixed along the way, in case the *why*
behind a decision matters to you. See `docs/BUILD_LOG.md`.
