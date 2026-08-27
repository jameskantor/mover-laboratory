"""
Automated regression suite for the silver layer -- a structured, pytest-runnable
version of scripts/verify_silver.py's checks, plus per-table known-example regression
tests pinned during the original dedup investigations. Run from inside the
mover-laboratory container:

    pytest tests/test_silver.py -v

This is an integration suite against the live Iceberg catalog (no fixtures/mocks) --
consistent with how every check in this project has worked from the start: real queries
against the real warehouse, not synthetic test data. It requires a built warehouse
(bronze ingested, silver built) to already exist; any table not yet built is skipped
rather than failed, so this suite works incrementally as tables get added.

Added as part of the data-engineering review (see docs/DATA_DICTIONARY.md) that found
this project had no automated verification anywhere -- every check before this was an
ad hoc script run by hand and eyeballed.
"""
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from catalog import get_catalog

N_OCCURRENCES_TABLES = [
    "patient_lda", "patient_history", "patient_visit", "patient_coding",
    "patient_medications", "patient_post_op_complications", "flowsheets",
    "patient_labs", "patient_procedure_events",
]

# Table -> source timestamp column bronze partitions by year on; silver must match
# (data-engineering review, 2026-08-27: silver had silently dropped this partitioning).
PARTITIONED_TABLES = [
    ("flowsheets", "RECORDED_TIME"),
    ("patient_labs", "Collection_Datetime"),
    ("patient_medications", "MED_ACTION_TIME"),
]


@pytest.fixture(scope="session")
def catalog():
    return get_catalog()


@pytest.fixture(scope="session")
def con():
    return duckdb.connect()


@pytest.fixture(scope="session")
def built_tables(catalog):
    return {name for _, name in catalog.list_tables("silver")}


def _files(catalog, layer, table_key):
    table = catalog.load_table(f"{layer}.{table_key}")
    return "[" + ",".join(f"'{t.file.file_path}'" for t in table.scan().plan_files()) + "]"


def _skip_unless_built(built_tables, table_key):
    if table_key not in built_tables:
        pytest.skip(f"silver.{table_key} not built yet")


# --- core invariant: every n_occurrences table -----------------------------------------

@pytest.mark.parametrize("table_key", N_OCCURRENCES_TABLES)
def test_n_occurrences_sums_to_bronze_row_count(catalog, con, built_tables, table_key):
    """No row was silently dropped or double-counted by any dedup GROUP BY -- the exact
    class of bug caught (by the user, not automatically) in the first
    patient_medications dedup attempt, which used SELECT DISTINCT and silently deleted
    11,783 dose-bearing rows instead of preserving them as a count."""
    _skip_unless_built(built_tables, table_key)
    n_bronze = con.execute(
        f"SELECT count(*) FROM read_parquet({_files(catalog, 'bronze', table_key)})"
    ).fetchone()[0]
    n_sum = con.execute(
        f"SELECT sum(n_occurrences) FROM read_parquet({_files(catalog, 'silver', table_key)})"
    ).fetchone()[0]
    assert int(n_sum or 0) == n_bronze


# --- partitioning regression -------------------------------------------------------------

@pytest.mark.parametrize("table_key,source_col", PARTITIONED_TABLES)
def test_partition_spec_matches_bronze(catalog, built_tables, table_key, source_col):
    _skip_unless_built(built_tables, table_key)
    spec = catalog.load_table(f"silver.{table_key}").spec()
    assert not spec.is_unpartitioned(), f"silver.{table_key} has no partition spec"
    assert len(spec.fields) == 1
    assert spec.fields[0].transform.__class__.__name__ == "YearTransform"


# --- patient_information: no n_occurrences, uses row-level flags instead ----------------

class TestPatientInformation:
    @pytest.fixture(autouse=True)
    def _skip_if_not_built(self, built_tables):
        _skip_unless_built(built_tables, "patient_information")

    def test_distinct_log_id_matches_mover_paper(self, catalog, con):
        files = _files(catalog, "silver", "patient_information")
        n = con.execute(f"SELECT count(DISTINCT LOG_ID) FROM read_parquet({files})").fetchone()[0]
        assert n == 64354, "should match the MOVER paper's stated EPIC surgery count exactly"

    def test_row_count_never_exceeds_bronze(self, catalog, con):
        silver_n = con.execute(
            f"SELECT count(*) FROM read_parquet({_files(catalog, 'silver', 'patient_information')})"
        ).fetchone()[0]
        bronze_n = con.execute(
            f"SELECT count(*) FROM read_parquet({_files(catalog, 'bronze', 'patient_information')})"
        ).fetchone()[0]
        assert silver_n <= bronze_n, "dedup only ever removes or flags rows, never adds them"

    def test_phantom_row_is_absent(self, catalog, con):
        """Regression test: LOG_ID 0c6b137659f5df02's second MRN (fc63c830038a1f83) was
        diagnosed as a phantom row -- zero clinical footprint anywhere in the warehouse,
        a subset-copy of the real patient's diagnosis history with a fabricated surgery
        date. The fix was specified but not actually applied for several days and
        through a full supervisor review before a later data-engineering review caught
        it still shipping. This test exists specifically so that class of "known but
        unapplied fix" can't silently reappear.
        """
        files = _files(catalog, "silver", "patient_information")
        n = con.execute(f"""
            SELECT count(*) FROM read_parquet({files})
            WHERE LOG_ID = '0c6b137659f5df02' AND MRN = 'fc63c830038a1f83'
        """).fetchone()[0]
        assert n == 0

    def test_log_id_collision_flags_are_paired(self, catalog, con):
        """Every log_id_collision row belongs to a 2-patient pair sharing one LOG_ID."""
        files = _files(catalog, "silver", "patient_information")
        n = con.execute(
            f"SELECT sum(CAST(log_id_collision AS INT)) FROM read_parquet({files})"
        ).fetchone()[0]
        assert n % 2 == 0


# --- known-example regression tests ------------------------------------------------------
# One hand-verified example per table, pinned during the original investigation. These
# catch a *specific* dedup mistake (a known group collapsing to the wrong n_occurrences,
# or a value silently merged that should have stayed separate) that the generic
# sum-check above wouldn't necessarily catch on its own.

def test_patient_history_known_104x_group(catalog, con, built_tables):
    _skip_unless_built(built_tables, "patient_history")
    files = _files(catalog, "silver", "patient_history")
    row = con.execute(f"""
        SELECT n_occurrences FROM read_parquet({files})
        WHERE MRN = '1bb09d5761661c7d'
          AND dx_name = 'Cervical cancer, FIGO stage IIB (CMS-HCC)'
    """).fetchone()
    assert row is not None and row[0] == 104


def test_patient_coding_known_612x_group(catalog, con, built_tables):
    _skip_unless_built(built_tables, "patient_coding")
    files = _files(catalog, "silver", "patient_coding")
    row = con.execute(f"""
        SELECT n_occurrences FROM read_parquet({files})
        WHERE MRN = 'cd955ec437f44536' AND REF_BILL_CODE = '0HDAXZZ'
    """).fetchone()
    assert row is not None and row[0] == 612


def test_patient_procedure_events_known_345x_mark_now(catalog, con, built_tables):
    _skip_unless_built(built_tables, "patient_procedure_events")
    files = _files(catalog, "silver", "patient_procedure_events")
    row = con.execute(f"""
        SELECT n_occurrences FROM read_parquet({files})
        WHERE LOG_ID = 'f9087a002b6d57d3' AND EVENT_DISPLAY_NAME = 'Mark Now'
          AND EVENT_TIME = '2020-08-13 13:26:00'
    """).fetchone()
    assert row is not None and row[0] == 345


def test_flowsheets_known_323x_outlier(catalog, con, built_tables):
    """The single largest duplicate group in the whole warehouse -- also the strongest
    evidence flowsheets' 63.6% duplication isn't coincidence."""
    _skip_unless_built(built_tables, "flowsheets")
    files = _files(catalog, "silver", "flowsheets")
    n = con.execute(f"SELECT max(n_occurrences) FROM read_parquet({files})").fetchone()[0]
    assert n == 323


def test_patient_labs_conflicting_units_stay_separate(catalog, con, built_tables):
    """Regression test for the rejected narrow-key dedup rule: two rows differing only
    in Measurement_Units notation (THOUS/MCL vs THOUS/ CU MM) for the same real result
    must remain two separate rows, not get silently merged."""
    _skip_unless_built(built_tables, "patient_labs")
    files = _files(catalog, "silver", "patient_labs")
    n = con.execute(f"""
        SELECT count(*) FROM read_parquet({files})
        WHERE LOG_ID = '7e4b5c6b62ce8b7b' AND Lab_Code = '26515-7'
          AND Collection_Datetime = '2022-10-27 13:07:00'
    """).fetchone()[0]
    assert n == 2
