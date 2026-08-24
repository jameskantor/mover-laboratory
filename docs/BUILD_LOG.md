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

Next per the checklist: every other table is gated on a duplicate-row audit that hasn't
been run yet (`patient_history`, `patient_visit`, `patient_coding`, `patient_medications`,
`patient_post_op_complications`) — one of those audits is the natural next step before
more of silver can be built.
