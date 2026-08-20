"""Exposes the bronze Iceberg tables over DuckDB's Quack remote protocol, so Windows-side
tools (DBeaver, or another DuckDB process) can query them over the network instead of
reading files directly -- the container is where the data and the query engine live.

Quack is beta (shipped 2026-05-12, stabilizes with DuckDB v2.0 in Sept 2026): expect
breaking changes. See docs/BUILD_LOG.md for why this is a prototype, not yet "the" pattern.

Run with: docker run --rm -p 9494:9494 -v "${PWD}:/work" -v mover-warehouse:/work/iceberg_warehouse
    mover-laboratory:latest python /work/scripts/quack_server.py
(or just use quack.ps1 from the project root)

Native DuckDB CLI test, once running:
    ATTACH 'quack:localhost:9494?token=mover-lab-dev-token' AS bronze (TYPE quack);
    SELECT COUNT(*) FROM bronze.flowsheets;
"""
import os
import time

import duckdb

WAREHOUSE = os.environ.get("MOVER_WAREHOUSE_DIR", "/work/iceberg_warehouse") + "/bronze"
TOKEN = os.environ.get("QUACK_TOKEN", "mover-lab-dev-token")
TABLES = [
    "patient_information", "patient_history", "patient_visit", "patient_coding",
    "patient_post_op_complications", "patient_lda", "patient_procedure_events",
    "patient_labs", "patient_medications", "flowsheets",
]

con = duckdb.connect()
con.execute("INSTALL iceberg; LOAD iceberg; INSTALL quack; LOAD quack;")
# GLOBAL, not session-scoped: quack_serve() executes incoming remote queries in a context
# that doesn't inherit this session's plain SET, so the view's iceberg_scan() fails
# version-guessing unless this is global.
con.execute("SET GLOBAL unsafe_enable_version_guessing = true;")
for t in TABLES:
    con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM iceberg_scan('{WAREHOUSE}/{t}')")

result = con.execute(
    "CALL quack_serve('quack:0.0.0.0:9494', allow_other_hostname => true, token => ?)",
    [TOKEN],
).fetchall()

print("Quack server started, serving views:", ", ".join(TABLES), flush=True)
print("Listen info:", result, flush=True)
print(f"Dev token (fixed, local-testing only): {TOKEN}", flush=True)
print("Windows client example:", flush=True)
print(f"  ATTACH 'quack:localhost:9494?token={TOKEN}' AS bronze (TYPE quack);", flush=True)

while True:
    time.sleep(3600)
