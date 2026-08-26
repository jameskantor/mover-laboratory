"""
Builds silver-layer tables from bronze. Unlike bronze ingestion (append-only, resumable
per source file), each silver table is fully recomputed from bronze on every run --
silver is derived data, so "rebuild from scratch" is simpler and safer than incremental
patching. Run once per table:

    python scripts/build_silver.py --table patient_information
    python scripts/build_silver.py --all

See docs/DATA_DICTIONARY.md's "Silver-layer design checklist" for the rationale behind
every transform below -- this script is the implementation, that doc is the walkthrough.
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).parent))
from catalog import get_catalog
from silver_schemas import SILVER_TABLES

LOG_DIR = Path("/work/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "build_silver.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("build_silver")


def arrow_type_for(iceberg_field):
    from pyiceberg.types import (
        StringType, LongType, DoubleType, TimestampType, BooleanType, ListType,
    )
    t = iceberg_field.field_type
    if isinstance(t, StringType):
        return pa.string()
    if isinstance(t, LongType):
        return pa.int64()
    if isinstance(t, DoubleType):
        return pa.float64()
    if isinstance(t, TimestampType):
        return pa.timestamp("us")
    if isinstance(t, BooleanType):
        return pa.bool_()
    if isinstance(t, ListType):
        elem = t.element_type
        if isinstance(elem, StringType):
            return pa.list_(pa.string())
        raise ValueError(f"unhandled list element type {elem}")
    raise ValueError(f"unhandled type {t}")


def bronze_files(catalog, table_key):
    table = catalog.load_table(f"bronze.{table_key}")
    files = [t.file.file_path for t in table.scan().plan_files()]
    return "[" + ",".join(f"'{f}'" for f in files) + "]"


def ensure_silver_table(catalog, table_key):
    identifier = f"silver.{table_key}"
    try:
        return catalog.load_table(identifier)
    except Exception:
        return catalog.create_table(identifier, schema=SILVER_TABLES[table_key])


def write_silver_table(catalog, table_key, df):
    schema = SILVER_TABLES[table_key]
    pa_fields = [pa.field(f.name, arrow_type_for(f), nullable=not f.required) for f in schema.fields]
    pa_schema = pa.schema(pa_fields)
    ordered_cols = [f.name for f in schema.fields]
    df = df[ordered_cols]
    arrow_tbl = pa.Table.from_pandas(df, schema=pa_schema, preserve_index=False)

    table = ensure_silver_table(catalog, table_key)
    table.overwrite(arrow_tbl)  # full recompute each run, not an incremental append
    log.info(f"[{table_key}] wrote {len(df):,} rows to silver.{table_key}")


# --- patient_information --------------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "Silver-layer design checklist" -> patient_information
# and "MRN corruption and patient-level linkage" for the full derivation):
#   1. Normalize: trim every string column, map the one confirmed DISCH_DISP wording
#      variant (code 69, mid-dataset Epic dictionary update) to its canonical text.
#   2. SELECT DISTINCT collapses the 1,364 exact-duplicate LOG_IDs automatically.
#   3. Of the 7 LOG_IDs still duplicated after that:
#      - 3 are real LOG_ID collisions across different patients (MRN differs AND every
#        other column differs) -- both rows are kept, flagged log_id_collision.
#      - 1 is an MRN-corruption artifact (MRN differs but every other column is
#        identical, and the differing MRN values are the scientific-notation corrupted
#        kind) -- collapsed to 1 row, flagged mrn_corrupt like any other corrupted MRN.
#      - 3 are genuine same-encounter conflicting values (MRN same, something else
#        differs, e.g. DISCH_DISP_C 15 vs 20) -- tie-broken to 1 row via a fixed,
#        reproducible ORDER BY over every column, flagged has_conflicting_duplicate.
_PATIENT_INFORMATION_SQL = """
WITH normalized AS (
  SELECT DISTINCT
    LOG_ID, MRN,
    DISCH_DISP_C,
    CASE WHEN DISCH_DISP = 'Designated Disaster Alternative Care Site'
         THEN 'Designated Disaster Alternate Care Site' ELSE trim(DISCH_DISP) END AS DISCH_DISP,
    HOSP_ADMSN_TIME, HOSP_DISCH_TIME, LOS, ICU_ADMIN_FLAG, SURGERY_DATE,
    BIRTH_DATE AS age_years,
    -- LOG_ID bd14c293acb63fcc's HEIGHT (8'2.3" / ~250cm) is physically implausible and
    -- nulled per DATA_DICTIONARY.md; LOG_ID 8944ca07ff7952b2's 7'7" is extreme but real
    -- and kept -- it just isn't special-cased, since it parses through the regex fine.
    CASE WHEN LOG_ID = 'bd14c293acb63fcc' THEN NULL
         WHEN regexp_matches(trim(HEIGHT), '^\\d+''\\s\\d+(\\.\\d+)?$')
         THEN split_part(trim(HEIGHT), '''', 1)::DOUBLE * 12
              + regexp_replace(trim(HEIGHT), '^\\d+''\\s', '')::DOUBLE
         ELSE NULL END AS height_in,
    WEIGHT * 0.0283495 AS weight_kg,  -- ounces (EPIC dataset) -> kg
    SEX, trim(PRIMARY_ANES_TYPE_NM) AS PRIMARY_ANES_TYPE_NM,
    ASA_RATING_C, ASA_RATING, PATIENT_CLASS_GROUP, PATIENT_CLASS_NM,
    trim(PRIMARY_PROCEDURE_NM) AS PRIMARY_PROCEDURE_NM,
    IN_OR_DTTM, OUT_OR_DTTM, AN_START_DATETIME, AN_STOP_DATETIME
  FROM read_parquet({files})
),
tagged AS (
  SELECT *,
    count(*) OVER (PARTITION BY LOG_ID) AS grp_n,
    count(DISTINCT MRN) OVER (PARTITION BY LOG_ID) AS grp_n_mrn,
    count(DISTINCT (DISCH_DISP_C, DISCH_DISP, HOSP_ADMSN_TIME, HOSP_DISCH_TIME, LOS,
                    ICU_ADMIN_FLAG, SURGERY_DATE, age_years, height_in, weight_kg, SEX,
                    PRIMARY_ANES_TYPE_NM, ASA_RATING_C, PATIENT_CLASS_GROUP, PATIENT_CLASS_NM,
                    PRIMARY_PROCEDURE_NM, IN_OR_DTTM, OUT_OR_DTTM, AN_START_DATETIME, AN_STOP_DATETIME))
      OVER (PARTITION BY LOG_ID) AS grp_n_distinct_nonmrn
  FROM normalized
),
classified AS (
  SELECT *,
    (grp_n > 1 AND grp_n_mrn > 1 AND grp_n_distinct_nonmrn > 1) AS log_id_collision,
    (grp_n > 1 AND grp_n_mrn > 1 AND grp_n_distinct_nonmrn = 1) AS mrn_dup_row,
    (grp_n > 1 AND grp_n_mrn = 1) AS has_conflicting_duplicate,
    row_number() OVER (PARTITION BY LOG_ID ORDER BY MRN, DISCH_DISP_C, DISCH_DISP,
                        HOSP_ADMSN_TIME, HOSP_DISCH_TIME, LOS, ICU_ADMIN_FLAG, SURGERY_DATE,
                        age_years, height_in, weight_kg, SEX, PRIMARY_ANES_TYPE_NM,
                        ASA_RATING_C, PATIENT_CLASS_GROUP, PATIENT_CLASS_NM,
                        PRIMARY_PROCEDURE_NM, IN_OR_DTTM, OUT_OR_DTTM, AN_START_DATETIME,
                        AN_STOP_DATETIME) AS rn
  FROM tagged
)
SELECT
  LOG_ID, MRN,
  regexp_matches(MRN, '[Ee][+-]') AS mrn_corrupt,
  log_id_collision, has_conflicting_duplicate,
  DISCH_DISP_C, DISCH_DISP, HOSP_ADMSN_TIME, HOSP_DISCH_TIME, LOS, ICU_ADMIN_FLAG,
  SURGERY_DATE, age_years, (age_years = 90) AS age_capped, height_in, weight_kg,
  SEX, PRIMARY_ANES_TYPE_NM, ASA_RATING_C, ASA_RATING, PATIENT_CLASS_GROUP,
  PATIENT_CLASS_NM, PRIMARY_PROCEDURE_NM, IN_OR_DTTM, OUT_OR_DTTM, AN_START_DATETIME,
  AN_STOP_DATETIME
FROM classified
WHERE log_id_collision                       -- keep all rows for real collisions
   OR (mrn_dup_row AND rn = 1)                -- collapse mrn-corruption duplicates to 1 row
   OR (has_conflicting_duplicate AND rn = 1)  -- tie-break conflicts to 1 row
   OR grp_n = 1                               -- normal single rows
"""


def build_patient_information(catalog):
    files = bronze_files(catalog, "patient_information")
    con = duckdb.connect()
    df = con.execute(_PATIENT_INFORMATION_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_expected = 64354  # matches the MOVER paper's stated EPIC surgery count exactly
    n_distinct = df["LOG_ID"].nunique()
    if n_distinct != n_expected:
        raise AssertionError(
            f"patient_information: expected {n_expected} distinct LOG_ID, got {n_distinct} "
            "-- dedup logic no longer matches the validated shape, stopping before write."
        )
    log.info(f"[patient_information] {len(df):,} rows, {n_distinct:,} distinct LOG_ID "
             f"(matches paper), mrn_corrupt={df['mrn_corrupt'].sum()}, "
             f"log_id_collision={df['log_id_collision'].sum()}, "
             f"has_conflicting_duplicate={df['has_conflicting_duplicate'].sum()}")

    write_silver_table(catalog, "patient_information", df)


# --- patient_lda ------------------------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "Silver-layer design checklist" -> patient_lda):
# 22.9% of bronze rows are the same physical device charted under two different LDA
# navigator categories at once -- identical on every column except Line_Group_Name (e.g.
# "Drain" + "Urinary Drainage" for one catheter). Collapse to one canonical row per real
# device event (grouping on every column except Line_Group_Name, after trimming), and
# carry every navigator category it was filed under in line_group_names instead of
# duplicating the row. multi_navigator flags exactly the rows this rule collapsed.
_PATIENT_LDA_SQL = """
WITH normalized AS (
  SELECT LOG_ID, MRN, trim(description) AS description,
         trim(properties_display) AS properties_display,
         trim(site) AS site, placement_instant, removal_instant,
         trim(flo_meas_name) AS flo_meas_name,
         trim(Line_Group_Name) AS Line_Group_Name
  FROM read_parquet({files})
)
SELECT LOG_ID, MRN, description, properties_display, site, placement_instant,
       removal_instant, flo_meas_name,
       list_sort(array_agg(DISTINCT Line_Group_Name)) AS line_group_names,
       (count(DISTINCT Line_Group_Name) > 1) AS multi_navigator
FROM normalized
GROUP BY LOG_ID, MRN, description, properties_display, site, placement_instant,
         removal_instant, flo_meas_name
"""


def build_patient_lda(catalog):
    files = bronze_files(catalog, "patient_lda")
    con = duckdb.connect()
    df = con.execute(_PATIENT_LDA_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    n_expected_min = 400000  # sanity floor -- dedup should collapse ~53K groups, not more
    if not (n_expected_min < len(df) < n_bronze):
        raise AssertionError(
            f"patient_lda: got {len(df):,} rows from {n_bronze:,} bronze rows -- outside "
            "the expected collapse range, dedup logic may no longer match the validated "
            "shape, stopping before write."
        )
    log.info(f"[patient_lda] {len(df):,} rows (from {n_bronze:,} bronze), "
             f"multi_navigator={df['multi_navigator'].sum():,}")

    write_silver_table(catalog, "patient_lda", df)


# --- patient_history --------------------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "Silver-layer design checklist" -> patient_history):
# 76% of bronze rows are "duplicates" of (mrn, diagnosis_code, dx_name) -- confirmed real
# (grep-verified against the raw source CSV, not an ingestion artifact) and mechanistic,
# not a data-quality bug: this table has no encounter id, so a chronic problem-list
# diagnosis gets re-exported once per clinical encounter that patient had. Duplication
# ratio scales ~1:1 with each patient's surgery count in patient_information (1.09x for a
# 1-surgery patient up to 52x for the one 41-surgery patient). Collapsed to one row per
# (mrn, diagnosis_code, dx_name), keeping the repeat count explicitly as n_occurrences
# rather than leaving it as an implicit, easy-to-lose row count.
_PATIENT_HISTORY_SQL = """
SELECT mrn, diagnosis_code, dx_name, count(*) AS n_occurrences
FROM read_parquet({files})
GROUP BY mrn, diagnosis_code, dx_name
"""


def build_patient_history(catalog):
    files = bronze_files(catalog, "patient_history")
    con = duckdb.connect()
    df = con.execute(_PATIENT_HISTORY_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    n_expected = 437721  # distinct (mrn, diagnosis_code, dx_name) groups, verified by hand
    if len(df) != n_expected:
        raise AssertionError(
            f"patient_history: expected {n_expected:,} distinct groups, got {len(df):,} "
            "-- dedup logic no longer matches the validated shape, stopping before write."
        )
    log.info(f"[patient_history] {len(df):,} rows (from {n_bronze:,} bronze), "
             f"max n_occurrences={df['n_occurrences'].max()}")

    write_silver_table(catalog, "patient_history", df)


# --- patient_visit ------------------------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "Silver-layer design checklist" -> patient_visit):
# 57% of bronze rows duplicate on (LOG_ID, mrn, diagnosis_code, dx_name) -- grep-verified
# real in the raw source CSV. Different mechanism than patient_history: this repeats
# WITHIN one encounter (one LOG_ID can carry 20+ copies of the same diagnosis), likely one
# row per clinical note/document that reiterates the visit's diagnosis list -- not
# resolvable further without a note id, which bronze doesn't have. Same treatment as
# patient_history: collapsed to one row per group, repeat count kept as n_occurrences.
_PATIENT_VISIT_SQL = """
SELECT LOG_ID, mrn, diagnosis_code, dx_name, count(*) AS n_occurrences
FROM read_parquet({files})
GROUP BY LOG_ID, mrn, diagnosis_code, dx_name
"""


def build_patient_visit(catalog):
    files = bronze_files(catalog, "patient_visit")
    con = duckdb.connect()
    df = con.execute(_PATIENT_VISIT_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    n_expected = 131455  # distinct (LOG_ID, mrn, diagnosis_code, dx_name) groups, verified by hand
    if len(df) != n_expected:
        raise AssertionError(
            f"patient_visit: expected {n_expected:,} distinct groups, got {len(df):,} "
            "-- dedup logic no longer matches the validated shape, stopping before write."
        )
    log.info(f"[patient_visit] {len(df):,} rows (from {n_bronze:,} bronze), "
             f"max n_occurrences={df['n_occurrences'].max()}")

    write_silver_table(catalog, "patient_visit", df)


# --- patient_coding -----------------------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "Silver-layer design checklist" -> patient_coding):
# Same mechanism as patient_history: no encounter id in this table, so a billing code
# gets re-exported once per clinical encounter that patient had -- grep-verified real
# against the raw source CSV (top group: 612 literal matching lines for one 34-surgery
# patient's ICD-10-PCS 0HDAXZZ). Collapsed to one row per (MRN, SOURCE_KEY, SOURCE_NAME,
# NAME, REF_BILL_CODE_SET_NAME, REF_BILL_CODE), repeat count kept as n_occurrences.
_PATIENT_CODING_SQL = """
SELECT MRN, SOURCE_KEY, SOURCE_NAME, NAME, REF_BILL_CODE_SET_NAME, REF_BILL_CODE,
       count(*) AS n_occurrences
FROM read_parquet({files})
GROUP BY MRN, SOURCE_KEY, SOURCE_NAME, NAME, REF_BILL_CODE_SET_NAME, REF_BILL_CODE
"""


def build_patient_coding(catalog):
    files = bronze_files(catalog, "patient_coding")
    con = duckdb.connect()
    df = con.execute(_PATIENT_CODING_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    n_expected = 1244633  # distinct group count, verified by hand
    if len(df) != n_expected:
        raise AssertionError(
            f"patient_coding: expected {n_expected:,} distinct groups, got {len(df):,} "
            "-- dedup logic no longer matches the validated shape, stopping before write."
        )
    log.info(f"[patient_coding] {len(df):,} rows (from {n_bronze:,} bronze), "
             f"max n_occurrences={df['n_occurrences'].max()}")

    write_silver_table(catalog, "patient_coding", df)


# --- patient_medications ------------------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "Silver-layer design checklist" -> patient_medications):
# Different mechanism than patient_history/patient_coding: this table HAS an encounter id
# (LOG_ID), and only 1.24% of bronze rows are duplicated (vs. 55-76% in the no-encounter-id
# tables), with max group size 15 (not hundreds) -- a MAR action charted more than once for
# the same encounter, not a per-encounter re-export pattern. Grep-verified the top group
# (15x) real against the raw source CSV. Collapsed via plain SELECT DISTINCT -- unlike
# patient_history's chronicity signal, this repeat count is charting noise, not carried
# forward as n_occurrences.
_PATIENT_MEDICATIONS_COLS = [
    "ENC_TYPE_C", "ENC_TYPE_NM", "LOG_ID", "MRN", "ORDERING_DATE", "ORDER_CLASS_NM",
    "MEDICATION_ID", "DISPLAY_NAME", "MEDICATION_NM", "START_DATE", "END_DATE",
    "ORDER_STATUS_NM", "RECORD_TYPE", "MAR_ACTION_NM", "MED_ACTION_TIME", "ADMIN_SIG",
    "DOSE_UNIT_NM", "MED_ROUTE_NM",
]
_PATIENT_MEDICATIONS_SQL = """
SELECT DISTINCT {cols}
FROM read_parquet({{files}})
""".format(cols=", ".join(_PATIENT_MEDICATIONS_COLS))


def build_patient_medications(catalog):
    files = bronze_files(catalog, "patient_medications")
    con = duckdb.connect()
    df = con.execute(_PATIENT_MEDICATIONS_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    n_expected = 27773144  # distinct row count, verified by hand
    if len(df) != n_expected:
        raise AssertionError(
            f"patient_medications: expected {n_expected:,} distinct rows, got {len(df):,} "
            "-- dedup logic no longer matches the validated shape, stopping before write."
        )
    log.info(f"[patient_medications] {len(df):,} rows (from {n_bronze:,} bronze)")

    write_silver_table(catalog, "patient_medications", df)


BUILDERS = {
    "patient_information": build_patient_information,
    "patient_lda": build_patient_lda,
    "patient_history": build_patient_history,
    "patient_visit": build_patient_visit,
    "patient_coding": build_patient_coding,
    "patient_medications": build_patient_medications,
}


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--table", choices=sorted(BUILDERS), help="build a single silver table")
    group.add_argument("--all", action="store_true", help="build every implemented silver table")
    args = ap.parse_args()

    catalog = get_catalog()
    tables = list(BUILDERS) if args.all else [args.table]
    for table_key in tables:
        log.info(f"=== building silver.{table_key} ===")
        try:
            BUILDERS[table_key](catalog)
        except Exception:
            log.exception(f"[{table_key}] FAILED")
            raise


if __name__ == "__main__":
    main()
