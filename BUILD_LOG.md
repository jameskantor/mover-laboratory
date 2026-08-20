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
duplicating it.

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
created a connection with URL `jdbc:quack://localhost:9494?token=mover-lab-dev-token`,
Test Connection succeeded. One false alarm along the way — an "instance already running"
error turned out to be the user starting a second Quack server (`quack.ps1`) while the
first detached one (`mover-quack`) was still up, colliding on port 9494; not a Quack or
driver problem, just two servers fighting over the same port.

**Result: the original ask from this whole thread of work is done.** Windows-side tools
(native CLI, DBeaver) reach the data over the network; the data and the query engine both
live in the container; nothing on the Windows side reads a warehouse file directly
anymore.
