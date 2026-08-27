"""
Independent post-build verification for the silver layer. This does NOT re-run
build_silver.py's own logic or trust its own assertions -- it re-derives structural
invariants directly against the live warehouse, the same way every dedup fix in this
project was manually verified during development, just automated and runnable in one
shot after any build:

    python scripts/verify_silver.py            # check every built silver table
    python scripts/verify_silver.py --table patient_medications

Exits non-zero if any check fails. This is not a full test suite (no CI, no fixtures --
see docs/DATA_DICTIONARY.md's data-engineering review for that gap), but it closes the
biggest hole: before this script existed, "does silver still match spec" could only be
answered by re-running the same ad hoc investigation queries by hand.

The one invariant checked for every n_occurrences-bearing table is deliberately
count-agnostic (sum(n_occurrences) == bronze row count) rather than a hardcoded literal
-- it stays meaningful even after bronze data changes, unlike build_silver.py's own
per-table n_expected assertions.
"""
import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from catalog import get_catalog

# Every silver table that carries an n_occurrences column -- i.e. every table except
# patient_information, which uses row-level boolean flags instead (checked separately).
N_OCCURRENCES_TABLES = [
    "patient_lda", "patient_history", "patient_visit", "patient_coding",
    "patient_medications", "patient_post_op_complications", "flowsheets",
    "patient_labs", "patient_procedure_events",
]


def _files(con, catalog, layer, table_key):
    table = catalog.load_table(f"{layer}.{table_key}")
    return "[" + ",".join(f"'{t.file.file_path}'" for t in table.scan().plan_files()) + "]"


def check_n_occurrences_sum(con, catalog, table_key):
    """sum(n_occurrences) must equal the bronze row count exactly -- if it doesn't, some
    rows were lost or double-counted, the exact failure mode caught (by the user, not
    automated) in the first patient_medications dedup attempt."""
    bronze_files = _files(con, catalog, "bronze", table_key)
    silver_files = _files(con, catalog, "silver", table_key)
    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({bronze_files})").fetchone()[0]
    n_sum = con.execute(f"SELECT sum(n_occurrences) FROM read_parquet({silver_files})").fetchone()[0]
    n_sum = int(n_sum) if n_sum is not None else 0
    ok = n_sum == n_bronze
    detail = f"sum(n_occurrences)={n_sum:,} vs bronze={n_bronze:,}"
    return ok, detail


def check_patient_information(con, catalog):
    """Bespoke checks for the one table with no n_occurrences column. Includes a
    regression check for the phantom-row bug (LOG_ID 0c6b137659f5df02) that was
    diagnosed and specified but shipped unfixed for days before a later review caught
    it -- this check exists specifically so that class of "known but unapplied fix"
    can't silently reappear."""
    silver_files = _files(con, catalog, "silver", "patient_information")
    bronze_files = _files(con, catalog, "bronze", "patient_information")

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({bronze_files})").fetchone()[0]
    row = con.execute(f"""
        SELECT count(*) AS n_rows, count(DISTINCT LOG_ID) AS n_distinct,
               sum(CAST(log_id_collision AS INT)) AS n_collision,
               sum(CAST(mrn_corrupt AS INT)) AS n_mrn_corrupt,
               sum(CAST(has_conflicting_duplicate AS INT)) AS n_conflict
        FROM read_parquet({silver_files})
    """).fetchone()
    n_rows, n_distinct, n_collision, n_mrn_corrupt, n_conflict = row

    checks = []
    checks.append((
        "distinct LOG_ID matches MOVER paper's stated surgery count",
        n_distinct == 64354, f"got {n_distinct:,}",
    ))
    checks.append((
        "row count is not larger than bronze (dedup only ever removes/flags, never adds)",
        n_rows <= n_bronze, f"silver={n_rows:,} bronze={n_bronze:,}",
    ))
    phantom = con.execute(f"""
        SELECT count(*) FROM read_parquet({silver_files})
        WHERE LOG_ID = '0c6b137659f5df02' AND MRN = 'fc63c830038a1f83'
    """).fetchone()[0]
    checks.append((
        "phantom row (0c6b137659f5df02/fc63c830038a1f83) is absent",
        phantom == 0, f"found {phantom} matching row(s)" if phantom else "absent, as expected",
    ))
    checks.append((
        "flag counts are internally consistent (all even, since every flagged row "
        "comes in matched pairs for log_id_collision, or is its own paired group)",
        n_collision % 2 == 0, f"log_id_collision={n_collision}",
    ))
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", help="verify a single table instead of every built table")
    args = ap.parse_args()

    catalog = get_catalog()
    con = duckdb.connect()
    built_tables = {name for _, name in catalog.list_tables("silver")}

    targets = [args.table] if args.table else sorted(built_tables)
    any_failed = False

    for table_key in targets:
        if table_key not in built_tables:
            print(f"SKIP  {table_key:32s} not built yet")
            continue

        if table_key == "patient_information":
            checks = check_patient_information(con, catalog)
        elif table_key in N_OCCURRENCES_TABLES:
            ok, detail = check_n_occurrences_sum(con, catalog, table_key)
            checks = [("sum(n_occurrences) matches bronze row count", ok, detail)]
        else:
            print(f"SKIP  {table_key:32s} no verification rule defined")
            continue

        for label, ok, detail in checks:
            status = "PASS" if ok else "FAIL"
            print(f"{status}  {table_key:32s} {label} -- {detail}")
            if not ok:
                any_failed = True

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
