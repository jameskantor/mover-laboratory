# Build Log

This is the project's lab notebook — decisions, experiments, and what we learned building
this, in the order we learned it. It exists so nothing gets re-litigated or re-discovered,
not as a tutorial.

**This is separate from the goal of the published repo.** `README.md` documents how to
stand up your own copy of this lab against your own credentialed MOVER download — that's
the audience-facing contract, aimed at someone else's machine. This file documents how *we*
built ours on this one: false starts, benchmarks, protocol research, bugs found and fixed.
Skip it entirely if you just want to run the pipeline.

For the data-ingestion-specific log (row counts, schema decisions, type fixes per table),
see `DATA_DICTIONARY.md` → "Ingestion log" — entries below reference it rather than
duplicating it. For a candidate technology's comparison/evaluation *before* a decision
gets made (vs. the integration work once it's adopted, which belongs here), see
`TECH_EVALUATIONS.md`.

---

## 2026-08-19 — Bronze layer built

Designed bronze Iceberg schemas, set up a local PyIceberg `SqlCatalog`, wrote a resumable
ingestion driver, and ingested all 9 EMR tables + flowsheets (1,502,559,444 rows total,
every table row-count-verified against its source CSV). One bug hit and fixed along the
way: a `DoubleType` column (`ADMIN_SIG`) wasn't covered by the original manual type-list
config, causing a crash on a malformed value partway through `patient_medications` — fixed
by deriving type coercion from the schema instead of hand-maintained lists.

Full detail, per-table row counts, and the schema/type decisions: `DATA_DICTIONARY.md` →
"Ingestion log" and "EPIC dataset tables".

## 2026-08-20 — DBeaver / native Windows couldn't read the warehouse

**Problem:** the bronze warehouse queried fine from inside the container but was
unreadable from DBeaver / native Windows DuckDB.

**Root cause:** Iceberg bakes absolute warehouse paths into every manifest/snapshot file
at write time. The warehouse had been written as `file:///work/...` (the container's own
mount path) — fine from inside the container, but native Windows DuckDB strips exactly one
leading `/` and resolves the remainder relative to the current working directory, not any
drive root, so `/work/...` references were unresolvable from Windows regardless of CWD
tricks or junctions.

**Fix:** `entrypoint.sh` creates a symlink `/D:/Data_Science_Projects/Mover -> /work`
inside the container, and the catalog's warehouse points at
`file:///D:/Data_Science_Projects/Mover/iceberg_warehouse` — every baked-in path is then
already a valid Windows path once that one leading slash is stripped, from either side.
Required a full bronze re-ingestion (baked paths can't be patched in place). Verified from
native Windows DuckDB querying from `C:\` (a different drive than the data, to rule out
CWD-relative resolution) — exact row counts on both `patient_information` and
`flowsheets`.

Full detail: `DATA_DICTIONARY.md` → "Ingestion log", 2026-08-20 entry.

## 2026-08-20 — Researched DuckDB's client/server options for DBeaver

**Question:** can DBeaver (native Windows) connect to DuckDB compute running *inside* a
container over a network, instead of reading files directly, to actually satisfy "UI in
Windows, compute in containers" rather than just working around it?

**Findings:**
- DuckDB is an embedded, in-process engine, not a client-server database. DBeaver's DuckDB
  JDBC driver is file-path only — no host/port connection string, no remote mode
  ([DuckDB's own DBeaver guide](https://duckdb.org/docs/lts/guides/sql_editors/dbeaver)
  confirms this).
- DuckDB does have an official browser-based UI (`CALL start_ui()`, built by MotherDuck,
  default port 4213) — run inside a container, port-mapped out, and a Windows browser gets
  a real SQL workbench talking to compute that's genuinely in the container. Same shape as
  the existing Jupyter setup.
- DuckDB also has a first-party remote protocol, **Quack** (shipped 2026-05-12, stabilizing
  into DuckDB v2.0 in September 2026 — currently beta, "expect breaking changes"). A DuckDB
  session inside a container can call `quack_serve()` and expose everything that session
  can see — including our bronze Iceberg views — to remote clients over the network. A
  third-party JDBC driver (`quack-jdbc`, alpha) would let DBeaver connect via a `quack://`
  URL; the native DuckDB CLI can `ATTACH 'quack:host:port' AS db (TYPE quack)` directly,
  no extra driver needed.
- Confirmed our stack already clears Quack's version floor: both the `mover-iceberg`
  container and the native Windows DuckDB CLI are on DuckDB 1.5.5 (≥ 1.5.3 required).
  Confirmed the `quack` extension loads and exposes the expected functions
  (`quack_serve`, `quack_query`, `quack_server_list`, etc.) in the container.

**Decision:** worth prototyping for our own use, not yet something to document as *the*
pattern for others — the protocol itself says to expect breaking changes before it
stabilizes next month. Revisit after DuckDB v2.0 ships.

**Reframing that came out of this:** DBeaver's job here is row-level browsing, not EDA —
that's a much lighter workload than what Jupyter does. Whether it reads files directly or
goes through Quack matters less than for actual EDA/training compute.

## 2026-08-20 — Made the warehouse path portable

**Problem:** `WAREHOUSE_DIR` in `scripts/catalog.py` was hardcoded to
`/D:/Data_Science_Projects/Mover/iceberg_warehouse` — literally this machine's folder path.
Since Iceberg bakes that path into every manifest file at write time, this wasn't just a
runtime inconvenience: it meant the *persisted warehouse itself* was welded to one specific
machine. Anyone else cloning the repo and running it against their own MOVER download
unmodified would have gotten this exact literal path baked into their data too, regardless
of where they actually mounted their files — it would just break.

**Fix:**
- `catalog.py`: `WAREHOUSE_DIR` now defaults to a portable `/work/iceberg_warehouse`
  (matching how `CATALOG_DB` already worked), reading `MOVER_WAREHOUSE_DIR` to opt into
  the Windows-drive-letter mode instead.
- `entrypoint.sh`: the symlink is now conditional on a `WINDOWS_HOST_PATH` env var rather
  than unconditionally creating `/D:/Data_Science_Projects/Mover`. No env var, no symlink
  — nothing Windows-specific happens by default.
- `scripts/shell.py`, `query.ps1`, `jupyter.ps1` updated to match; the two `.ps1` entry
  points now pass `WINDOWS_HOST_PATH` / `MOVER_WAREHOUSE_DIR` explicitly, so this
  machine's day-to-day usage (and DBeaver compatibility) is unchanged.

**Verified:** rebuilt the image, re-ran a query against the existing (already-ingested)
warehouse through the new configurable path — `patient_information` still returns
65,728 rows.

## 2026-08-20 — Benchmark: D: bind mount vs. Docker-native volume

**Question:** is "keep data close to compute" a real, measurable cost here, or just Docker
Desktop's general reputation for slow bind mounts?

**Method:** the same DuckDB aggregation query — `GROUP BY FLO_DISPLAY_NAME` over all
1,440,918,933 rows of `bronze.flowsheets` — run 3 times each against two storage backends:

- **A — current setup:** the bronze warehouse on `D:\...\iceberg_warehouse`, bind-mounted
  into the container (what `query.ps1` / `jupyter.ps1` use today).
- **B — Docker-native volume:** a throwaway copy of `bronze/` in a Docker-managed volume
  (lives inside Docker Desktop's own WSL2-backed storage, not on a Windows drive path),
  read by a container with no D: mount involved for the query itself.

Both produced identical results (`SpO2` 101,560,255 / `Resp` 97,802,415 / `Pulse`
80,896,755 — top groups matched exactly), confirming it's a fair comparison, not a
different query.

**Results:**

| | run 1 (cold) | run 2 | run 3 |
|---|---|---|---|
| A — D: bind mount | 4.24s | 3.14s | 3.34s |
| B — Docker-native volume | 0.60s | 0.51s | 0.51s |

The native volume was **~6x faster**, and the gap held on the very first (coldest) query
for each side — not just after caches warmed up.

**Caveat:** the volume copy happened immediately before its test, so its OS page cache had
a head start the bind-mount side didn't get. A 6x gap on the first query for each side is
larger than cache-priming alone would likely explain, but it isn't a fully cache-neutral
test (would need a cache-drop between runs to isolate that completely).

**Status:** this is a real, measured case for moving the warehouse into container-native
storage for actual EDA/training workloads — not yet acted on. Doing so would mean DBeaver
loses direct file access entirely (no Windows-visible path left to point at), making a
network path (Quack, once it matures, or a browser-based tool) mandatory rather than
optional for that use case. Decision on the full migration is pending the "one strong
container" architecture direction and Quack's stabilization.

## 2026-08-20 — Acted on the benchmark: renamed image, moved the warehouse, wired up Quack

Followed through on the direction the benchmark supported.

**Renamed** `mover-iceberg:latest` → `mover-laboratory:latest` (retag, no rebuild needed —
the name undersold the EDA/Quack role once it wasn't just an ingestion tool).

**Considered re-ingesting fresh into the new storage** (to get manifest paths shaped like
`/work/iceberg_warehouse` instead of the vestigial `/D:/Data_Science_Projects/Mover/...`
shape) but talked it out of scope: `cp` is a byte-for-byte copy, and the benchmark had
already proven the copy-then-symlink approach works and is fast. Re-ingesting an hour of
work for a cosmetic path difference wasn't worth it — started it, then stopped and
switched to copying once this was clear.

**Moved the warehouse** (`bronze/`, `catalog.db`, `_ingestion_status.json` — not the loose
`mover.duckdb`/`ui_session.duckdb` files sitting in the same directory, leftover from
DBeaver connection testing and not part of the Iceberg structure) into a new Docker-native
volume, `mover-warehouse`, via `cp -r`. Verified by exact row count after the copy:
`patient_information` → 65,728, `flowsheets` → 1,440,918,933 — both exact matches.
`query.ps1` / `jupyter.ps1` now mount `mover-warehouse` at `/work/iceberg_warehouse`
(nested inside the `/work` bind mount) instead of relying on the D: bind mount for the
warehouse specifically; the `WINDOWS_HOST_PATH`/`MOVER_WAREHOUSE_DIR` env vars are kept
(pointed through the same symlink mechanism) since the existing manifests still bake in
the D:-colon-path shape — cosmetic, not a correctness issue.

**Corruption/performance check, asked directly and answered with evidence, not just
reasoning:** `cp` cannot alter Parquet/Avro/JSON content — no transformation step exists
that could introduce drift. The only real copy-specific risk is a truncated/interrupted
write, which the exact row-count match on the largest table (1.44B rows) rules out — a
truncated Parquet file fails to open or undercounts, it doesn't silently return the exact
right number. Performance: the benchmark *is* this exact mechanism already measured at 6x
faster, not a theoretical analogue.

**Wired up `quack_server.py` + `quack.ps1`** — registers the 10 bronze views and calls
`quack_serve()`, serving them over the network with a fixed dev token
(`mover-lab-dev-token`, local-testing only). Ran as a detached container (`docker run -d
--name mover-quack`) rather than folded into `jupyter.ps1`, since it needs to answer
external client connections independent of any single foreground session — an `--rm -it`
container dies when you're done with it. (Open question: fold it into the same running
container as Jupyter, so one container serves both roles, instead of a dedicated one —
not yet decided.)

**Bug hit and fixed:** `SET unsafe_enable_version_guessing = true` at session scope (the
setting `shell.py`/`query.ps1` already relied on) doesn't carry into how `quack_serve()`
executes *incoming remote* queries against the views — they run in a context that doesn't
inherit the launching session's plain `SET`. Fixed with `SET GLOBAL
unsafe_enable_version_guessing = true` instead.

**Verified end-to-end** from the native Windows DuckDB CLI (not just from inside another
container) — `ATTACH`/`quack_query` against `quack:localhost:9494` returned
`1,440,918,933` for `flowsheets`, an exact match, over the network, through the container,
off the volume-backed warehouse.

**DBeaver confirmed working** (2026-08-20, same session): registered the `quack-jdbc.jar`
driver (class `com.gizmodata.quack.jdbc.sql.QuackDriver`) in DBeaver's Driver Manager,
created a connection, Test Connection succeeded. One false alarm along the way — an
"instance already running" error turned out to be the user starting a second Quack server
(`quack.ps1`) while the first detached one (`mover-quack`) was still up, colliding on port
9494; not a Quack or driver problem, just two servers fighting over the same port.

**Follow-up bug: Database Navigator showed "No items" under Views**, even though the
connection worked and SQL editor queries against it returned correct data. Root cause:
the connection URL had no `{database}` segment (`jdbc:quack://localhost:9494?token=...`),
and the tree was browsing a catalog named `mover` that doesn't exist server-side.
Confirmed the real catalog/schema directly against the server:
`SELECT current_catalog(), current_schema()` → `memory`, `main`; `SHOW DATABASES` → only
`memory` exists. Fixed by adding the database segment to the URL:
`jdbc:quack://localhost:9494/memory?token=mover-lab-dev-token` — tree populates
correctly now. **Use this URL form, not the one without `/memory`, for any new DBeaver
connection to this server.**

**Result: the original ask from this whole thread of work is done.** Windows-side tools
(native CLI, DBeaver) reach the data over the network, including the Database Navigator
tree, not just ad hoc SQL; the data and the query engine both live in the container;
nothing on the Windows side reads a warehouse file directly anymore.

## 2026-08-20 — Folded Quack into jupyter.ps1; cleaned up the redundant warehouse copy

Closed out the open items from the previous entry.

**Folded the standalone Quack server into `jupyter.ps1`'s container**, rather than keeping
it as a separate dedicated one. `scripts/start_lab.sh` starts `quack_server.py` in the
background and `jupyter lab` in the foreground, both in the same container — one
persistent container serving notebook + network-query roles together, both ports
(`8888`, `9494`) published. Verified with a real test: brought up the merged container,
confirmed a Quack query returns the correct `patient_information` count (65,728) and
JupyterLab responds (HTTP 302 on `/`), before removing the old standalone `mover-quack`
container and `quack.ps1`. `quack_server.py`'s docstring updated to point at
`jupyter.ps1`/`start_lab.sh` instead.

**Deleted the redundant pre-migration warehouse copy** at `D:\...\iceberg_warehouse\`
(4.9GB — `bronze/`, `catalog.db`, `_ingestion_status.json`, plus the two stray
`mover.duckdb`/`ui_session.duckdb` files from the earlier DBeaver-direct-file testing,
also now obsolete since DBeaver connects via Quack). Confirmed safe first: `query.ps1` /
`jupyter.ps1` mount `mover-warehouse` as a *nested volume* at `/work/iceberg_warehouse`,
which shadows whatever's physically at that path on the host inside the container's mount
namespace — so nothing running actually reads the D: copy anymore, regardless of the
`WINDOWS_HOST_PATH`/`MOVER_WAREHOUSE_DIR` symlink chain still pointing through it.
Verified after deleting: `flowsheets` still returns `1,440,918,933` via the same
`iceberg_scan()` path used throughout this log.

**Left open, not decided:** how the pytorch/training container will actually reach the
warehouse data. Genuinely unresolved (not just deprioritized) — no design started.

## 2026-08-20 — VS Code remote-kernel setup; XSRF bug; default CMD

**Connected a Windows-native notebook client (VS Code) to the container as a remote
kernel** — "Existing Jupyter Server" pointed at `http://localhost:8888/`, rather than
using the browser UI. First attempt looked connected but cells stayed queued forever.
Diagnosed with a quick cell (`platform.system()`) that turned out to still say
`Windows jamespc` — VS Code had silently fallen back to a local interpreter, not the
remote one, because nothing was actually running on port 8888 at the time.

**Second attempt, real bug:** with `jupyter.ps1` actually running and VS Code correctly
pointed at it, cells *still* hung. Container logs showed the real cause: `403 POST
/api/sessions ... '_xsrf' argument missing from POST`. Jupyter Server's CSRF protection
expects a browser-style cookie handshake that VS Code's non-browser Jupyter client
doesn't do, so every session-creation request was silently rejected and cells just queued
forever with no visible error on the VS Code side. Fixed with
`--ServerApp.disable_check_xsrf=True` in `scripts/start_lab.sh` (safe here — no auth on
this server either way, loopback/LAN dev use only). Verified the fix directly by
replaying the same POST curl would send and confirming `403` → `201`, before telling the
user to reconnect.

**Set a default `CMD`** (`sh /work/scripts/start_lab.sh`) and `EXPOSE 8888 9494` in the
`Dockerfile`, so a bare `docker run` (mounts only, no explicit command) launches the full
lab. Costs nothing for existing usage — `query.ps1` and ingestion already pass their own
explicit commands, which override `CMD` regardless. Verified with a real bare `docker run`
(no command argument) — Quack came up and answered a query correctly on first try.

**Created `EDA/patient_age_los_distributions.ipynb`** — first real notebook against the
warehouse: age and length-of-stay distributions from `bronze.patient_information`
(65,728 surgeries; age mean 55.1/range 17–90; LOS mean 7.4 days, median 2, right-skewed
per the log-scale plot). Whether this goes into git or stays local-only is still an open
question — aggregate stats/histograms, not patient-level rows, but flagged rather than
decided unilaterally given the data sensitivity throughout this project.

## 2026-08-23 — Stray `D:/` directory from GPU container testing

Found a literal `D:\Data_Science_Projects\Mover\D:\Data_Science_Projects\Mover\
iceberg_warehouse\` directory tree sitting on disk — empty, harmless, already covered by
`.gitignore`'s `iceberg_warehouse/` pattern, but confusing to see in an editor's file
tree. Root cause: `entrypoint.sh`'s `WINDOWS_HOST_PATH` symlink logic (`mkdir -p
$(dirname /$WINDOWS_HOST_PATH) && ln -s /work "/$WINDOWS_HOST_PATH"`) is meant to create
that symlink inside the container's own filesystem only. Running the `Dockerfile.gpu`
container with the whole project root bind-mounted at `/work` plus that same env var
caused it to materialize as real nested directories on the Windows host instead — not
reproduced by the normal `mover-laboratory` container path (bronze's warehouse lives in
the `mover-warehouse` Docker volume, not through this mechanism at all). Deleted; if it
recurs, avoid bind-mounting the full project root together with `WINDOWS_HOST_PATH` set.

## 2026-08-22/23 — GPU BSOD investigation: RAPIDS cuDF evaluation dropped, GPU training parked

**What happened:** starting the "GPU-accelerated dataframe prep" evaluation (RAPIDS
`cuDF` via `scripts/bench_gpu_dataprep.py` / `Dockerfile.gpu`, see
`docs/TECH_EVALUATIONS.md`) reliably triggered full-system Windows BSODs — bugcheck
`0x133 DPC_WATCHDOG_VIOLATION` — on this laptop (Lenovo Legion Pro 7 16IAX10H, RTX 5090
Laptop GPU). Four crashes across two days, all under real GPU load from the same
benchmark. Root-caused via `cdb.exe -z <dump> -c "!analyze -v"` to a stuck DPC, first
twice directly in `nvlddmkm.sys` (the NVIDIA driver), later once in `dxgmms2.sys`
(Windows' own GPU scheduler, one layer up the same call chain). Confirmed via web
research this is a widely-reported issue on RTX 5090 laptops (NVIDIA forum thread with
215 dumps, 100% consistent signature, since Aug 2025) with a documented root cause: the
BIOS/EC fails to respond to a GPU power-state transition request in time
(`ACPI.sys` DPC latency ~34,000µs), leaving the GPU firmware and the OS driver
disagreeing about its power state, and the driver thread blocks forever waiting for a
state change that never comes.

**Mitigations tried, in order, none of which eliminated the issue:**
1. BIOS update (was already current — `Q7CN78WW`, confirmed via Lenovo Vantage).
2. Driver downgrade from generic NVIDIA Studio `610.88` to the Lenovo-qualified `592.01`
   — nontrivial on its own: NVIDIA App's driver auto-update silently reinstalled
   `610.88` minutes after the first downgrade attempt (traced via its own logs), and
   separately Windows' driver-ranking silently no-op'd a same-rank reinstall attempt
   (had to force it via Device Manager → "let me pick from a list"). Disabled NVIDIA
   App's auto-update (`NvBackend/config.xml`) so the downgrade would actually stick.
3. PCIe ASPM (Link State Power Management): Maximum power savings → Off.
4. Intel(R) Graphics power plan (relevant to Optimus/hybrid-graphics dGPU switching):
   Balanced → Maximum Performance.
5. Display sleep timeout: 10min/3min → Never (display-off is itself a GPU power-state
   transition, matching the root-cause mechanism directly).
6. GPU Working Mode (BIOS-level MUX switch): Hybrid → dGPU-only, i.e. removed
   Optimus-style dGPU power-gating entirely, the most directly-targeted fix attempted.

**Result: the dGPU-only-mode retest produced a worse failure than any prior crash** — a
full unresponsive system freeze with no BSOD, no crash dump, and no auto-recovery,
requiring a hard power-off. Confirmed via Windows Event Log (Event 41/6008, "system
stopped responding... unexpected shutdown") that no new bugcheck occurred — the DPC
watchdog that had reliably caught (and recovered from) every prior incident didn't fire
this time, consistent with an even lower-level freeze (possibly SMI/firmware-level,
halting interrupt/timer delivery entirely) than what the watchdog can detect.

**Decision: stopped here rather than continuing to escalate mitigations on a laptop that
now has one crash mode with no safety net.** Reframed the actual need instead of
continuing to chase the bug:
- **RAPIDS/cuDF is not needed** — DuckDB already handles the target workload (aggregations
  over the 1.44B-row `flowsheets` table) in under 3 seconds with no GPU. Every crash this
  week came from validating a tool that wasn't blocking anything. Verdict: **rejected**,
  see `docs/TECH_EVALUATIONS.md`.
- **GPU model training (PyTorch) is a separate, real dependency** — and notably, this
  same GPU trained a CNN successfully as recently as 2026-01-25 (a `pytorch/pytorch`
  Docker container, clean exit), 7 months before any of this week's crashes. That's
  evidence this is a recent regression (plausibly the NVIDIA-App auto-driver-update
  behavior found and disabled during this investigation moving the machine onto a bad
  driver branch), not an inherent hardware limitation. Whether sustained real PyTorch
  training hits the same bug was never tested — only `cuDF` was. **Parked** as an open
  question for a dedicated future session, gated on the still-undecided tabular-vs-
  sequence model architecture choice that determines whether GPU training is even
  needed yet.

## 2026-08-23 — Silver build started: `scripts/build_silver.py`, `patient_information` done

**Pattern decision:** silver is a script (`scripts/build_silver.py`), not a notebook —
matches `ingest.py`'s shape (`--table <name>` / `--all`), so "reproduce this" means
"run one command" the same way bronze ingestion does. Unlike bronze (append-only,
resumable per source file), each silver table is **fully recomputed from bronze on every
run** via `table.overwrite(...)` — simpler and safer than incremental patching for
derived data. The judgment calls behind each transform live in
`docs/DATA_DICTIONARY.md`'s "Silver-layer design checklist", cross-referenced from the
script rather than duplicated in code comments.

**`patient_information` implemented and verified.** The headline piece was finally
resolving the 1,374 excess-`LOG_ID` rows found back on 2026-08-21 (row content was never
actually inspected until this session): 1,364 exact duplicates + 4 whitespace/wording
variants collapse via `SELECT DISTINCT` after normalizing; of the remaining 7 truly
divergent `LOG_ID`s, 3 are real cross-patient `LOG_ID` collisions (kept, both rows,
flagged `log_id_collision` — `LOG_ID` is genuinely not unique for these), 1 is an
`MRN`-corruption artifact (collapsed, flagged `mrn_corrupt`), and 3 are genuine
same-encounter value conflicts (tie-broken via a fixed `ORDER BY`, flagged
`has_conflicting_duplicate`). Also implemented: the `mrn_corrupt` flag from the MRN
corruption investigation, `BIRTH_DATE`→`age_years`+`age_capped`, `HEIGHT`→`height_in`
(with the one implausible-outlier row nulled), `WEIGHT`→`weight_kg`, and a global
string-trim pass. Production run: 64,357 rows / **64,354 distinct `LOG_ID` — matches the
MOVER paper's stated surgery count exactly** — `mrn_corrupt`=37, `log_id_collision`=6,
`has_conflicting_duplicate`=3, all matching the values validated by hand beforehand.
Also added a `silver` Iceberg namespace to `scripts/catalog.py` (created alongside
`bronze` if missing) and `scripts/silver_schemas.py` for silver table schemas, mirroring
`schemas.py`'s bronze pattern.

Remaining tables not yet built — see `docs/DATA_DICTIONARY.md`'s checklist for what's
left per table, several still gated on duplicate-row audits that haven't been run yet
(`patient_history`, `patient_visit`, `patient_coding`, `patient_medications`,
`patient_post_op_complications`).

## 2026-08-24 — Silver build: `patient_lda` done

Second table implemented (`build_patient_lda` in `scripts/build_silver.py`), the other
one whose dedup rule was already fully resolved going in. Collapses the 22.9%
cross-navigator duplication found during the column audit — the same physical device
charted under two LDA navigator categories at once (e.g. `Drain` + `Urinary Drainage`
for one catheter), identical on every other column. Grouped on every column except
`Line_Group_Name` (after trimming strings), one canonical row per real device event;
renamed `Line_Group_Name` → `line_group_names` (a `list<string>` holding every navigator
category the device was filed under, rather than duplicating the row) with a
`multi_navigator` boolean flagging which rows this collapsed.

Needed one addition to the silver-schema machinery: `scripts/silver_schemas.py`'s
`_schema()` helper only handled flat field types, since `patient_information` didn't
need anything nested. Reworked it to take a shared `_IdGen` field-id counter so a column
can be a callable that mints its own nested element id — `ListType` needs one distinct
from the column's own field id. `build_silver.py`'s `arrow_type_for()` got the matching
`ListType` → `pa.list_(pa.string())` case.

Production run: 412,312 rows (from 465,801 bronze), `multi_navigator`=53,218. Row count
lands close to but not exactly the 106,534-duplicated/53,174-groups figure from the
original column audit — that estimate didn't trim whitespace first, so a handful of
near-duplicate groups that differed only by stray whitespace get correctly merged here
that the untrimmed scan had counted as distinct. Verified by reading the written table
back and spot-checking sample `multi_navigator` rows' `line_group_names` lists.

## 2026-08-25 — Silver build: `patient_history` done; duplicate-row mechanism confirmed real

Ran the `patient_history` duplicate-row audit that's been outstanding since the
bronze-layer column audit. **76% of rows (734,073/970,741) were duplicates on
`(mrn, diagnosis_code, dx_name)`** — initially looked alarming (user's reaction: "I
can't believe these rows are all dupes — all columns are exactly the same?"), so
verified rather than assumed before building anything.

Investigation: duplication ratio scales almost exactly 1:1 with how many surgeries that
patient has in `patient_information` (1.09× for a 1-surgery patient up to 52× for the
single 41-surgery patient) — a smoking gun, since this table has no encounter id at all
(no `LOG_ID`, no date). Each diagnosis on a patient's problem list gets re-recorded once
per clinical encounter, so a chronic diagnosis on a heavily-operated patient legitimately
appears dozens of times. Confirmed this isn't an ingestion bug by grepping the raw source
CSV directly for the largest offender (`mrn 1bb09d5761661c7d` / `Cervical cancer, FIGO
stage IIB (CMS-HCC)`, 104 occurrences) — found exactly 104 literal matching lines in
`patient_history.csv`, so the duplication is genuinely in MOVER's exported data, not
introduced by this pipeline. Real EHR problem-list behavior, not a data-quality defect.

Side finding along the way: `diagnosis_code` is not a stable key on its own (e.g.
`V45.89` maps to 489 different `dx_name` values — it's a generic "postprocedural status"
code) — `dx_name` carries the real specificity, worth knowing for anyone joining on
`diagnosis_code` alone later.

**Implementation** (`build_patient_history` in `scripts/build_silver.py`): collapsed to
one row per `(mrn, diagnosis_code, dx_name)`, keeping the repeat count explicitly as
`n_occurrences` rather than leaving it implicit in row count (loses information silently
if someone later drops to `SELECT DISTINCT`). One correction caught during
implementation: the initially-assumed target of "~236,668 rows" (from an earlier
back-of-envelope estimate) turned out to be the count of never-duplicated singleton
groups only, not the true distinct-group total — the actual correct figure, confirmed by
a fresh live query before hardcoding the validation assertion, is **437,721**. Production
run: 437,721 rows (from 970,741 bronze), `sum(n_occurrences)` matches the bronze row
count exactly, the known 104× group collapses correctly on verification.

Next per the checklist: `patient_visit`, `patient_coding`, `patient_medications`, and
`patient_post_op_complications` still each need their own duplicate-row audit before
more of silver can be built.

## 2026-08-25 — Silver build: `patient_visit` done

Ran the `patient_visit` duplicate-row audit — 57% of bronze rows (125,405/219,257)
duplicated on `(LOG_ID, mrn, diagnosis_code, dx_name)`. Looked like the same shape as
`patient_history` at first glance (same columns), but the mechanism is different: this
table has `LOG_ID`, and the duplication happens **within one encounter** rather than
across encounters — the largest group is one `LOG_ID` with 756 rows / 27 distinct
diagnoses, one diagnosis (`Hemorrhagic shock (CMS-HCC)`) repeated 54 times. Grep-verified
against the raw source CSV (54 literal matching lines) before trusting it, same as
`patient_history`. Working theory: one row per clinical note/document within the
encounter that reiterates the visit's diagnosis list — can't confirm further since
bronze carries no note/document id for this table.

Applied the same fix as `patient_history`: `build_patient_visit` in
`scripts/build_silver.py` collapses to one row per `(LOG_ID, mrn, diagnosis_code,
dx_name)`, keeping the repeat count as `n_occurrences`. Production run: 131,455 rows
(from 219,257 bronze), `sum(n_occurrences)` matches bronze exactly, known 54× group
collapses correctly on verification.

Next: `patient_coding`, `patient_medications`, and `patient_post_op_complications` still
need their own duplicate-row audits.

## 2026-08-26 — Silver build: `patient_coding` done

Ran the `patient_coding` duplicate-row audit — 55% of bronze rows (1,124,044/2,033,948)
duplicated on `(MRN, SOURCE_KEY, SOURCE_NAME, NAME, REF_BILL_CODE_SET_NAME,
REF_BILL_CODE)`. Same mechanism as `patient_history`: this table has no encounter id
either, so a billing code re-exports once per clinical encounter that patient had —
duplication correlates with each patient's surgery count the same way (the top group,
612 occurrences of ICD-10-PCS `0HDAXZZ`, belongs to a 34-surgery patient). Grep-verified
against the raw source CSV (612 literal matching lines) before trusting it, same
discipline as the prior two tables. One incidental finding along the way: `SOURCE_KEY`
looked like it might be a row id (it's typed `long`), but it's actually a 7-value
code-set-type lookup key — worth knowing before anyone assumes it's unique per row.

Applied the same fix: `build_patient_coding` in `scripts/build_silver.py` collapses to
one row per group, repeat count kept as `n_occurrences`. Production run: 1,244,633 rows
(from 2,033,948 bronze), `sum(n_occurrences)` matches bronze exactly, known 612× group
collapses correctly on verification.

Next: `patient_medications` and `patient_post_op_complications` still need their own
duplicate-row audits before more of silver can be built.

## 2026-08-26 — Silver build: `patient_medications` done

Ran the `patient_medications` duplicate-row audit — a different shape than the last two
tables. Only 1.24% of bronze rows (345,530/27,961,524) were duplicated, and the
mechanism is different too: this table has an encounter id (`LOG_ID`), unlike
`patient_history`/`patient_coding`, so the pervasive no-encounter-id re-export
explanation doesn't apply here. Instead this is a MAR (medication administration) action
getting charted more than once for the same encounter — the top group, 15 occurrences of
a `MAR Hold` for `sodium chloride 0.9% infusion` on one specific `LOG_ID`, grep-confirmed
as 15 literal matching lines in the raw source CSV, same discipline as the prior tables.
Max group size across the whole table is 15, nowhere near `patient_history`'s 104 or
`patient_coding`'s 612 — consistent with this being MAR-charting noise rather than a
structural re-export pattern.

Given the repeat count here doesn't carry real chronicity information the way it does in
`patient_history`, `build_patient_medications` collapses via a plain `SELECT DISTINCT`
rather than adding an `n_occurrences` column — the first table in this series to use that
simpler treatment instead. Production run: 27,773,144 rows (from 27,961,524 bronze), the
known 15× group collapses to exactly 1 row on verification.

## 2026-08-27 — `patient_medications` dedup revised: was silently dropping dosed rows

The `SELECT DISTINCT` fix from the day before was wrong, caught by the user before it
went further: `ADMIN_SIG` (dose) is populated on 95.7% of `Given` rows, 60% of `Rate
Verify`, 69% of `New Bag`, 93% of `Rate Change` — not logistics-only actions as assumed.
11,509 duplicate `Given` groups with a real dose lost 11,783 rows to the blind collapse.
The user also raised a competing theory worth testing on its own merits: could the
duplicates represent a fluid infusion genuinely continuing at that minute, rather than
an artifact to discard?

Tested both questions directly against real charting timelines (not assumed either way).
Pulled two full real MAR histories end to end. Both show duplication clustering in
**clean multi-day blocks** (every action doubled/tripled for days, then a sharp
transition to clean singletons) rather than scattered across the timeline — and in one
case, `MAR Hold`/`MAR Unhold` (one-time, discrete actions) each repeat 5× at the
**identical second** (`14:31:24`, `17:02:48`). A state-change action can't be 5 genuine
separate real events at one instant, which rules out the infusion-continuing theory as
the general explanation. The block shape instead points at an export/ETL artifact —
likely an overlapping date-range extraction window in MOVER's own per-encounter export,
the same family of issue as the confirmed `flowsheets` re-emission mechanism. One case's
block also recurred identically across 5 different `LOG_ID`s all sharing one `MRN` — one
patient's continuous ICU stay spanning 5 procedures 13 days apart, the medication
order's MAR history apparently attributed to (and independently duplicated within)
multiple of that admission's encounters. Full writeup: `DATA_DICTIONARY.md` → "MAR
duplicate-row investigation."

**Revised fix:** switched from `SELECT DISTINCT` to the collapse-to-`n_occurrences`
pattern already used for `patient_history`/`patient_visit`/`patient_coding`, so the
duplicate record is preserved as a count rather than deleted. Added an explicit warning
in the schema comment and docs: `n_occurrences` reflects export duplication, not repeat
administration — never multiply `ADMIN_SIG` by it to compute a dose or fluid total.
Dropped and rebuilt `silver.patient_medications` (schema changed, needed a fresh table).
Verified: 27,773,144 rows, `sum(n_occurrences)` matches bronze exactly, known 15× group
shows `n_occurrences=15`.

Process change adopted going forward, saved to memory
(`feedback_dedup_approval.md`): explain the duplicate-row theory and get explicit
approval before implementing any table's dedup fix, rather than building first and
explaining after the fact via commit message.

Next: `patient_post_op_complications` is the last table still gated on its own
duplicate-row audit.

## 2026-08-27 — Silver build: `patient_post_op_complications` done, bronze duplicate-row audit complete

Ran the last outstanding duplicate-row audit. 79% of bronze rows (203,945 total)
duplicated — but 98% of that is a single generic administrative flag,
`AN AQI POST-OP COMPLICATIONS`, always null-valued, repeated up to 49× for one
encounter. Same re-emission-per-note mechanism already confirmed for `patient_visit`:
this table has `LOG_ID` yet still duplicates heavily within one encounter, and
`CONTEXT_NAME` splits into `ENCOUNTER`/`ORDER`/`NOTE` — consistent with "one row per
document/note that references this element," not a data defect.

Before proposing a fix, explicitly checked for this table's version of the
`patient_medications` mistake: is there a quantity/dose-like column that a blind
collapse could silently drop? User asked directly ("so are we missing a note or free
text field?") — checked and confirmed `SMRTDTA_ELEM_VALUE` is the only content-bearing
column and it's already part of the dedup key; there's no separate note-text column
`CONTEXT_NAME='NOTE'` might be pointing at. Also confirmed the small remainder of real
complications (21 groups, 42 rows — `Unplanned Postoperative Ventillation`,
`Respiratory Failure`, `Hypotension (SBP<80 for 10 min)`) that duplicate the same way
are grep-real, not an ingestion artifact. Unlike medications, there's no dose/quantity
column here at risk — a complication's presence doesn't need multiplying into a total,
so this table carried none of the silent-data-loss risk the prior one did.

Theory explained and approved before implementing, per the new process from the
previous entry. `build_patient_post_op_complications` collapses to one row per
`(LOG_ID, MRN, Element_Name, CONTEXT_NAME, Element_abbr, SMRTDTA_ELEM_VALUE)`, repeat
count kept as `n_occurrences`. Verified: 84,776 rows (from 203,945 bronze),
`sum(n_occurrences)` matches bronze exactly, the real "Unplanned Postoperative
Ventillation" duplicate shows `n_occurrences=2`.

**This closes out the duplicate-row audit across all 10 bronze tables** (see
`DATA_DICTIONARY.md`'s "Duplicate-row audit" tracking table for the full summary) and
the silver build now covers 7 of 10 tables (`patient_information`, `patient_lda`,
`patient_history`, `patient_visit`, `patient_coding`, `patient_medications`,
`patient_post_op_complications`). Remaining: `flowsheets` (mechanism confirmed, dedup
recommended but not yet implemented — see "Duplicate-row audit" above) and
`patient_labs` (0.38% exact duplicates, confirmed real, not yet implemented).

## 2026-08-27 — Independent supervisor review of all 7 dedup fixes; 3 follow-ups applied

Ran an independent review (a fresh agent, not the one that implemented the fixes) of
every dedup decision made across the 7 tables — the point being to catch mistakes the
implementer wouldn't catch on their own, the way the user caught the `patient_medications`
dose-loss issue rather than it being self-identified. Methodology: re-read the docs and
`build_silver.py`/`silver_schemas.py`, then independently re-queried bronze and silver
live (not trusting the documented numbers) and grep-verified several claims against the
raw source CSVs.

**Result: 5 of 7 tables sound with no issues** (`patient_history`, `patient_visit`,
`patient_coding`, `patient_medications`, `patient_post_op_complications`) — every
top-line count, `sum(n_occurrences)` reconciliation, and specific cited example
reproduced exactly. Two follow-ups were found and, after explaining the theory and
getting explicit approval (per the process from the previous entry), fixed:

1. **`patient_information`** — the 3 `has_conflicting_duplicate` rows were being
   tie-broken to 1 row via a fixed `ORDER BY`, silently discarding a real, differing
   value (age 64 vs 66; discharge disposition 15 vs 20) with no recovery mechanism. Same
   category of mistake as the `patient_medications` issue, just 3 rows instead of
   thousands. Fixed to match `log_id_collision`'s treatment: keep both rows, flag.
   Production run: 64,360 rows (was 64,357), 64,354 distinct `LOG_ID` unchanged,
   `has_conflicting_duplicate`=6 (was 3). Verified all 3 pairs now retain both values.
2. **`patient_lda`** — the only table in the batch with no `n_occurrences`-equivalent
   column; a separate small set of plain exact duplicates (not the documented
   cross-navigator mechanism) were being silently absorbed by
   `array_agg(DISTINCT Line_Group_Name)` with no record they existed. No information was
   actually at risk (the collapsed rows are identical), but it broke the "always
   preserve the count" discipline used everywhere else. Added `n_occurrences`. Own
   re-verification against live silver output found 197 groups / 206 excess rows are
   plain duplicates — different from the reviewer's cited 231/271, not reconciled
   further since it doesn't change the fix; `sum(n_occurrences)`=465,801 matches bronze.
3. **Doc-only**: the "MAR duplicate-row investigation" writeup framed the `patient_medications`
   5×/5× `MAR Hold`/`MAR Unhold` example as an isolated single-infusion case; the
   reviewer found it's actually one instance of a 44+-medication simultaneous mass-hold
   event on that encounter. Updated the writeup — strengthens the existing conclusion,
   no code change.

Both `silver.patient_information` and `silver.patient_lda` were dropped and rebuilt
(schema changes needed fresh tables, same as `patient_medications`'s revision earlier).

## 2026-08-27 — Global casing/trim pass across all 7 built tables

Closed out the two long-standing cross-table checklist items: `LOG_ID`/`MRN` casing
consistency and a universal whitespace-trim pass. `patient_history`/`patient_visit`
renamed their lowercase `mrn` column to `MRN`, matching the other five tables — pure
rename, no data or row-count change, confirmed by directly re-checking column names
across all 7 tables after rebuild. Trim was applied to every categorical string column
in the 5 tables that hadn't already been trimmed (`patient_information`/`patient_lda`
did their own full trim during their original builds).

**Real methodology mistake, caught by the rebuild itself, not by a human catching it
this time.** Before implementing, checked impact by comparing each column's `DISTINCT`
count before/after `trim()` in isolation — `patient_coding.NAME` and
`patient_medications.DISPLAY_NAME` looked like they'd merge 1 and 15 groups
respectively, so the plan (and its approval) stated specific expected row-count drops.
Implemented and ran the rebuild — both tables' assertions failed, row counts came back
completely unchanged. The per-column check was measuring the wrong thing: a column's
distinct-value count dropping after trim only proves *some* two rows *somewhere* in the
table share a whitespace-variant value — it says nothing about whether those two rows
are otherwise identical across the table's *full* dedup key (same `MRN`, same
`SOURCE_KEY`, etc.). They weren't — the "merges" were between unrelated rows for
different patients. Re-checked correctly against the full group-by tuple
(`count(DISTINCT (col1, col2, ...))` before/after) and got 0 real merges for both
tables, matching what the rebuild had already shown. Fixed both assertions back to the
original (unchanged) expected row counts and re-ran successfully.

Final state, verified directly against every table: all 7 built silver tables expose
`MRN` (uppercase, consistent), and every row count matches its pre-pass value exactly —
`patient_information` 64,360, `patient_lda` 412,312, `patient_history` 437,721,
`patient_visit` 131,455, `patient_coding` 1,244,633, `patient_medications` 27,773,144,
`patient_post_op_complications` 84,776. Trimming was still worth applying even at zero
merge impact — it cleans standalone whitespace values with no downside — but the
row-count-change claim in the original approved plan was wrong, and is corrected here
rather than left standing.

Remaining from the original global checklist item: `flowsheets.FLO_NAME`/`UNITS` still
need this same trim pass whenever that table gets built (unbuilt, and the pattern there
is large-scale — `"Vital Signs "` alone is 27.7% of the entire table, so unlike the 5
tables just fixed, that one is very likely to have real merge impact).

## 2026-08-27 — Silver build: `flowsheets` done — the last table, and the biggest by far

Built the largest and most consequential table last, deliberately: `flowsheets` is
1,440,918,933 bronze rows, ~2.5 orders of magnitude bigger than the next-largest table
built so far (`patient_medications`, 28M). Every other silver table so far was built by
pulling the full deduped result into a pandas `DataFrame` in one shot
(`write_silver_table`) — that pattern flatly does not work here; a ~659M-row result with
string/timestamp columns would not fit in memory as a `DataFrame`.

Added `write_silver_table_streaming` to `scripts/build_silver.py`: DuckDB executes the
same normalize-then-`GROUP BY` query used everywhere else, but instead of `.fetchdf()`
the result is pulled as an Arrow `RecordBatchReader` (`to_arrow_reader(batch_rows=
10_000_000)`) and each batch is cast to the target schema and appended to the Iceberg
table directly — peak memory is bounded by one 10M-row batch, not the ~659M-row total.
Validated the batch mechanics on a narrow real date-slice (`RECORDED_TIME < 2017-12-01`)
before running the real thing, catching two issues cheaply instead of expensively: a
Python `%`-format collision with the SQL's own `LIKE '%...%'` wildcards (switched to
`.format()`, matching every other builder's pattern), and `fetch_record_batch()` being
deprecated in this DuckDB version (1.5.5) in favor of `to_arrow_reader()`.

**Dedup rule**, same reasoning as always: mechanism for the 63.6% duplication (916M
rows) is still unconfirmed (see "Duplicate-row audit" in `DATA_DICTIONARY.md` — export
re-emission vs. device-feed retry, neither proven), but collapsing to `n_occurrences` is
recommended regardless of root cause, and it's cheap to bundle with the two
already-scoped standardization fixes into a single 1.44B-row pass rather than three
separate ones: `FLO_NAME` trimmed (`"Vital Signs "` alone was 27.7% of the whole table),
`UNITS` casing/typo variants normalized (`cmH20`→`cmH2O`, `l/min`→`L/min`, `ml`→`mL`),
and the 6-row CSV-escaping corruption nulled (any `UNITS` containing a comma or quote —
a real unit never has either).

Production run: 658,839,669 rows (from 1,440,918,933 bronze), took ~10 minutes end to
end. Verified: `sum(n_occurrences)` matches the bronze row count exactly (guaranteed by
`GROUP BY` semantics, cheap to check via a `sum()` over the much-smaller silver table
rather than re-scanning bronze); the known 323× outlier group (a `[REMOVED]`-tagged
static field re-emitted 323 times within one encounter) collapses to exactly
`n_occurrences=323`; 0 rows have the pre-normalization `"Vital Signs "` / `cmH20` /
`l/min` / `ml` variants remaining; 0 rows have a comma or quote in `UNITS`.

**This completes the silver build for all 8 tables that have a resolved dedup rule** —
`patient_information`, `patient_lda`, `patient_history`, `patient_visit`,
`patient_coding`, `patient_medications`, `patient_post_op_complications`, and now
`flowsheets`. `patient_labs` (0.38% exact duplicates, confirmed real, dedup rule already
scoped) and `patient_procedure_events` (dedup rule still unresolved — bug vs. legitimate
one-row-per-item charting convention, open since 2026-08-21) are the two tables left.

## 2026-08-27 — Silver build: `patient_labs` done, original dedup scope revised

Picked up `patient_labs` where the earlier column audit had left it — a narrow 5-column
key (`LOG_ID, MRN, Lab_Code, Observation_Value, Collection_Datetime`) had been scoped as
"the" dedup rule based on a 0.38%/109,995-row estimate. Checked it before implementing
blind, per the standing rule to explain the theory and get approval first: the narrow
key turned out to be unsafe. **86% of the groups it would flag as duplicates actually
diverge on columns it ignores** — 673 groups where `Reference_Range` genuinely differs
(e.g. one Potassium 4.10 result reported against two different reference ranges — a real
conflicting value, not a rounding/notation issue) and 43,060 groups where
`Abnormal_Flag` is `NULL` on one row and computed/filled on the other (the flag being
derived a moment after the result posts, not a duplicate). Collapsing on the narrow key
would have silently merged both categories into one row and thrown away real
information — the same class of mistake caught earlier in `patient_medications`.

Correctly re-scoped to the full 9-column exact match (every real column except
ingestion metadata): the true duplicate rate is **7,536 groups / 15,072 rows — 0.05% of
the table**, far smaller than the original estimate. Grep-confirmed a real example
(a `Measurement_Units` notation-variant pair, `THOUS/MCL` vs `THOUS/ CU MM`, same
Platelets result) as literal duplicate lines in the raw source CSV before trusting it.

`build_patient_labs` in `scripts/build_silver.py` collapses on the full 9-column key,
repeat count kept as `n_occurrences`; the 673 `Reference_Range` conflicts and 43,060
`Abnormal_Flag` completions are deliberately left as separate rows, untouched.
Production run: 29,071,808 rows (from 29,079,344 bronze), `sum(n_occurrences)` matches
bronze exactly, the cited `Measurement_Units` conflict correctly remains two rows.

Only `patient_procedure_events` is left — dedup rule still unresolved (bug vs. legitimate
one-row-per-item charting convention, open since 2026-08-21).

## 2026-08-27 — Silver build: `patient_procedure_events` done, all 10 tables built

Resolved the last open dedup question in the warehouse: whether `patient_procedure_events`'
size-2 duplicate pairs (93.8% of its duplicate groups, dominated by the event
`Two Anti-Emetics Administered`) were a legitimate one-row-per-drug charting convention
(the event name literally implies 2) or an export artifact, open since the original
column audit on 2026-08-21.

Tested and rejected the convention theory three ways: (1) only 60% of encounters with
that event show exactly 2 rows — 40% (12,670 encounters) show just 1, a handful show 3
or 4, inconsistent with a fixed "2 drugs = 2 rows" rule; (2) the duplication rate is
wildly uneven across event types — true singular checkpoints (`Anesthesia Start`,
`Sign In`, `Extubation`, etc.) sit at a uniform ~0.1–0.6% background rate (plausibly the
same low-level export noise seen elsewhere in this warehouse), while
`Two Anti-Emetics Administered` spikes to 74.2% of its own rows — two orders of
magnitude above that floor, with nothing about the event's name explaining either
number; (3) duplicate rows are byte-identical including `EVENT_TIME` to the minute —
two real, distinct drug-administration events landing on the exact same timestamp with
zero differentiating data is far more consistent with one action re-emitted than two
real events. Grep-confirmed both a real size-2 pair (2 literal identical lines in the
raw CSV) and the table's extreme `Mark Now` outlier (346 literal lines for that
`LOG_ID`, 345 of them at the identical timestamp — a stuck-click-style charting glitch,
not real events).

`build_patient_procedure_events` collapses to one row per `(LOG_ID, MRN,
EVENT_DISPLAY_NAME, EVENT_TIME, NOTE_TEXT)` — the full column set, nothing excluded
from the key — repeat count kept as `n_occurrences`. Production run: 604,364 rows (from
640,223 bronze), `sum(n_occurrences)` matches bronze exactly, the known 345× `Mark Now`
group collapses to exactly `n_occurrences=345`.

**This completes the silver build for all 10 bronze tables.** Remaining open items are
per-column, not dedup: sentinel handling (`patient_labs`' `9999999.0`, `patient_history`/
`patient_visit`'s `IMO0001`), semantic corrections (`patient_coding`'s CPT-vs-HCPCS
mislabeling, `patient_information`'s backwards `PATIENT_CLASS_NM` subtypes), and a
handful of standardization/missingness items — see `DATA_DICTIONARY.md`'s "Silver-layer
design checklist" for the full per-table list. Gold-layer design (feature tables for a
specific ML question) hasn't started.

## 2026-08-27 — Data-engineering review of the finished silver layer; 11 findings fixed

With all 10 tables built, asked for an independent architecture/operations review — not
dedup-logic correctness (the supervisor review a few commits back already covered that),
but the surrounding engineering: partitioning, schema-change handling, write-strategy
consistency, reproducibility docs, and whether a previously-diagnosed bug had actually
shipped fixed. Full writeup with all 11 findings: `docs/DATA_DICTIONARY.md` →
"Data-engineering review (2026-08-27)". The two that mattered most:

**A known bug had never actually been fixed.** Back when investigating `LOG_ID`
collisions, `0c6b137659f5df02`'s second `MRN` (`fc63c830038a1f83`) was diagnosed as a
phantom row — zero clinical footprint anywhere, a subset-copy of the real patient's
diagnosis history with a fabricated surgery date — and the fix (drop it, keep only the
real row) was specified in the checklist. It never got coded. It shipped in every
subsequent silver build, survived the supervisor review, and was still there when this
review checked the live table directly. Fixed now in `_PATIENT_INFORMATION_SQL`; the row
is filtered out before classification runs. `log_id_collision` dropped from 6 rows to 4,
total silver rows from 64,360 to 64,359, distinct `LOG_ID` unchanged at 64,354.
`scripts/verify_silver.py` checks for this specific row going forward, specifically so
"diagnosed but never applied" can't happen silently again.

**Silver had silently dropped bronze's partitioning.** `DATA_DICTIONARY.md`'s own
Architecture section states that `flowsheets`/`patient_labs`/`patient_medications` are
partitioned by year for query pruning — true in bronze, never carried into
`silver_schemas.py` when silver was written. Reinstated via a new
`partition_spec_for()` helper (looks up the source column's field id via
`Schema.find_field()` rather than hardcoding it, so it stays correct if column order
ever changes) — same source column and year transform as bronze for all three tables.

Applying a partition spec to an already-written table isn't possible after the fact, so
all three needed a full rebuild. That surfaced the next fix: `ensure_silver_table()` had
no handling for "the schema or partition spec changed since this table was created" at
all — three separate times this project (`patient_information`, `patient_lda`,
`patient_medications`, across earlier sessions), that hit a raw `pyarrow.ValueError`
requiring a manual `catalog.drop_table()` outside the script, with nothing documented
about it. Now it compares both the existing table's schema and partition spec against
the current definitions in `silver_schemas.py` and auto-drops/recreates on a mismatch,
logging what changed — confirmed working live: rebuilding `patient_labs` and
`patient_medications` both correctly logged `"partition spec doesn't match... dropping
and recreating"` and proceeded without manual intervention.

Also fixed while in there: a bare `except Exception` in `ensure_silver_table` (was
catching everything, not just "table doesn't exist," now catches
`pyiceberg.exceptions.NoSuchTableError` specifically); `patient_medications` (28M rows)
and `patient_labs` (29M rows) switched from a plain pandas round-trip to the same
streaming Arrow-batch writer `flowsheets` already used, with an explicit
`STREAMING_THRESHOLD_ROWS` (10M) constant so the next large table has a documented rule
instead of an ad hoc per-table decision; `README.md`'s Status section and
`DATA_DICTIONARY.md`'s top-level "Open items" checklist both updated (they'd drifted —
still said "Silver / gold: not started" days after silver finished); the
`n_occurrences`-and-never-multiply-`ADMIN_SIG`-by-it warning promoted from one inline
table row to its own subsection in the Architecture section; a cosmetic
`UserWarning: Delete operation did not match any records` silenced (harmless, but every
build was logging it as if something failed); a stale `silver.vitals` table-name
reference corrected to `silver.flowsheets`; and `scripts/verify_silver.py` added —
independent post-build verification (re-derives `sum(n_occurrences) == bronze row
count` for the 9 applicable tables, plus bespoke checks for `patient_information`
including a standing regression test for the phantom-row fix above), the first
automated check this project has had anywhere.

Rebuilt `patient_information` (64,359 rows, `log_id_collision`=4, verified via
`verify_silver.py`), `patient_labs` (29,071,808 rows, `sum(n_occurrences)` matches
bronze exactly), `patient_medications` (27,773,144 rows, same), and `flowsheets`
(re-running with the new partition spec applied — the 1.44B-row streaming build takes
~10 minutes).

Not done as part of this review: a real test suite / CI, and a from-scratch pipeline
re-run (bronze → silver → verify) to confirm the whole thing still reproduces end to end
after all of the above.
