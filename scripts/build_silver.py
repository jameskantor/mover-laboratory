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


def pa_schema_for(table_key):
    schema = SILVER_TABLES[table_key]
    pa_fields = [pa.field(f.name, arrow_type_for(f), nullable=not f.required) for f in schema.fields]
    return pa.schema(pa_fields)


def write_silver_table(catalog, table_key, df):
    pa_schema = pa_schema_for(table_key)
    ordered_cols = [f.name for f in SILVER_TABLES[table_key].fields]
    df = df[ordered_cols]
    arrow_tbl = pa.Table.from_pandas(df, schema=pa_schema, preserve_index=False)

    table = ensure_silver_table(catalog, table_key)
    table.overwrite(arrow_tbl)  # full recompute each run, not an incremental append
    log.info(f"[{table_key}] wrote {len(df):,} rows to silver.{table_key}")


def write_silver_table_streaming(catalog, table_key, sql, files, batch_rows=10_000_000):
    """Same full-recompute contract as write_silver_table, but for tables too large to
    round-trip through a pandas DataFrame (flowsheets: ~1.44B bronze rows). Streams the
    query result as Arrow record batches straight from DuckDB into Iceberg via chunked
    append() calls, so peak memory is bounded by batch_rows rather than the full result.
    """
    pa_schema = pa_schema_for(table_key)
    ordered_cols = [f.name for f in SILVER_TABLES[table_key].fields if f.name != "_silver_built_at"]

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='24GB'")
    result = con.execute(sql.format(files=files))
    reader = result.to_arrow_reader(batch_rows)

    table = ensure_silver_table(catalog, table_key)
    table.overwrite(pa.Table.from_batches([], schema=pa_schema))  # clear prior run's data

    now = datetime.now(timezone.utc)
    n_rows = 0
    n_batches = 0
    for batch in reader:
        batch_tbl = pa.Table.from_batches([batch])
        batch_tbl = batch_tbl.select(ordered_cols)
        batch_tbl = batch_tbl.append_column(
            "_silver_built_at", pa.array([now] * batch_tbl.num_rows, type=pa.timestamp("us"))
        )
        batch_tbl = batch_tbl.cast(pa_schema)
        table.append(batch_tbl)
        n_rows += batch_tbl.num_rows
        n_batches += 1
        if n_batches % 25 == 0:
            log.info(f"[{table_key}] streamed {n_rows:,} rows so far ({n_batches} batches)")

    log.info(f"[{table_key}] wrote {n_rows:,} rows to silver.{table_key} ({n_batches} batches)")
    return n_rows


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
WHERE log_id_collision           -- keep all rows for real collisions
   OR (mrn_dup_row AND rn = 1)   -- collapse mrn-corruption duplicates to 1 row
   OR has_conflicting_duplicate  -- keep all rows for genuine conflicts, flagged (revised
                                  -- 2026-08-27, supervisor review: the prior tie-break
                                  -- silently discarded a real alternative value -- e.g.
                                  -- age 64 vs 66, DISCH_DISP_C 15 vs 20 -- with no
                                  -- recovery path. Matches log_id_collision's treatment.
   OR grp_n = 1                  -- normal single rows
"""


def build_patient_information(catalog):
    files = bronze_files(catalog, "patient_information")
    con = duckdb.connect()
    df = con.execute(_PATIENT_INFORMATION_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_expected_distinct = 64354  # matches the MOVER paper's stated EPIC surgery count
    n_expected_rows = 64360  # 64357 + 3 extra rows from keeping both has_conflicting_duplicate rows
    n_distinct = df["LOG_ID"].nunique()
    if n_distinct != n_expected_distinct or len(df) != n_expected_rows:
        raise AssertionError(
            f"patient_information: expected {n_expected_distinct} distinct LOG_ID / "
            f"{n_expected_rows} rows, got {n_distinct} / {len(df)} -- dedup logic no "
            "longer matches the validated shape, stopping before write."
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
# duplicating the row. multi_navigator flags exactly the rows this collapsed for that
# reason. Separately (found 2026-08-27, supervisor review): 271 rows (231 groups) are
# plain exact duplicates -- identical Line_Group_Name too, not a cross-navigator split --
# that array_agg(DISTINCT...) also silently absorbs with no record they existed. Added
# n_occurrences (count(*) of the group) so every table in this batch preserves a repeat
# count rather than discarding multiplicity information silently, even where (as here)
# the collapsed rows carry no distinguishing information at risk.
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
       (count(DISTINCT Line_Group_Name) > 1) AS multi_navigator,
       count(*) AS n_occurrences
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
# Casing/trim pass (2026-08-27, global standardization): bronze's `mrn` renamed to `MRN`
# to match every other table (silent-join-failure risk otherwise); `diagnosis_code` and
# `dx_name` trimmed. Checked impact before applying: 0 merges for this table's columns
# -- purely cosmetic, no row-count change.
_PATIENT_HISTORY_SQL = """
SELECT MRN, diagnosis_code, dx_name, count(*) AS n_occurrences
FROM (
  SELECT mrn AS MRN, trim(diagnosis_code) AS diagnosis_code, trim(dx_name) AS dx_name
  FROM read_parquet({files})
)
GROUP BY MRN, diagnosis_code, dx_name
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
# Casing/trim pass (2026-08-27): `mrn` renamed to `MRN`; `diagnosis_code`/`dx_name`
# trimmed. Checked impact before applying: 0 merges -- purely cosmetic here.
_PATIENT_VISIT_SQL = """
SELECT LOG_ID, MRN, diagnosis_code, dx_name, count(*) AS n_occurrences
FROM (
  SELECT LOG_ID, mrn AS MRN, trim(diagnosis_code) AS diagnosis_code, trim(dx_name) AS dx_name
  FROM read_parquet({files})
)
GROUP BY LOG_ID, MRN, diagnosis_code, dx_name
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
# Trim pass (2026-08-27, global standardization): trimmed all 4 categorical string
# columns. Impact was checked incorrectly at first (comparing each column's DISTINCT
# count before/after trim in isolation suggested NAME would merge 1 group -- but that
# only proves two rows somewhere share a whitespace-variant NAME, not that they're
# otherwise-identical rows in this table's 6-column dedup key). Correctly re-checked
# against the full group-by tuple: 0 real merges. Row count is unchanged, 1,244,633.
_PATIENT_CODING_SQL = """
SELECT MRN, SOURCE_KEY, SOURCE_NAME, NAME, REF_BILL_CODE_SET_NAME, REF_BILL_CODE,
       count(*) AS n_occurrences
FROM (
  SELECT MRN, SOURCE_KEY, trim(SOURCE_NAME) AS SOURCE_NAME, trim(NAME) AS NAME,
         trim(REF_BILL_CODE_SET_NAME) AS REF_BILL_CODE_SET_NAME,
         trim(REF_BILL_CODE) AS REF_BILL_CODE
  FROM read_parquet({files})
)
GROUP BY MRN, SOURCE_KEY, SOURCE_NAME, NAME, REF_BILL_CODE_SET_NAME, REF_BILL_CODE
"""


def build_patient_coding(catalog):
    files = bronze_files(catalog, "patient_coding")
    con = duckdb.connect()
    df = con.execute(_PATIENT_CODING_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    n_expected = 1244633  # unchanged by trim pass -- verified 0 real merges (see above)
    if len(df) != n_expected:
        raise AssertionError(
            f"patient_coding: expected {n_expected:,} distinct groups, got {len(df):,} "
            "-- dedup logic no longer matches the validated shape, stopping before write."
        )
    log.info(f"[patient_coding] {len(df):,} rows (from {n_bronze:,} bronze), "
             f"max n_occurrences={df['n_occurrences'].max()}")

    write_silver_table(catalog, "patient_coding", df)


# --- patient_medications ------------------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "MAR duplicate-row investigation" for the full
# writeup): CONFIRMED export artifact, not real clinical events. Duplicate groups cluster
# in contiguous multi-day blocks, and one-time actions (MAR Hold/Unhold) repeat at the
# identical second within those blocks -- rules out both charting noise and periodic
# infusion-continuation checks as the general explanation. The block pattern recurs
# identically across every LOG_ID belonging to one patient's single continuous
# multi-surgery admission, consistent with an overlapping date-range extraction window in
# MOVER's own per-encounter export. Collapsed to one row per group, repeat count kept as
# n_occurrences (same pattern as patient_history/patient_visit/patient_coding) --
# n_occurrences reflects export duplication, NEVER multiply ADMIN_SIG (dose) by it to
# compute a total; that's a deliberate gold-layer decision, not a silver default.
# Trim pass (2026-08-27, global standardization): trimmed all 9 categorical string
# columns (ID/key columns LOG_ID/MRN and numeric/timestamp columns left untouched).
# Impact was checked incorrectly at first (per-column DISTINCT-count reduction suggested
# 15 merges on DISPLAY_NAME -- but that only shows two rows SOMEWHERE share a
# whitespace-variant name, not that they're otherwise-identical within this table's full
# 17-column dedup key). Correctly re-checked against the full group-by tuple: 0 real
# merges. Row count is unchanged, 27,773,144.
_PATIENT_MEDICATIONS_COLS = [
    "ENC_TYPE_C", "ENC_TYPE_NM", "LOG_ID", "MRN", "ORDERING_DATE", "ORDER_CLASS_NM",
    "MEDICATION_ID", "DISPLAY_NAME", "MEDICATION_NM", "START_DATE", "END_DATE",
    "ORDER_STATUS_NM", "RECORD_TYPE", "MAR_ACTION_NM", "MED_ACTION_TIME", "ADMIN_SIG",
    "DOSE_UNIT_NM", "MED_ROUTE_NM",
]
_PATIENT_MEDICATIONS_TRIM_COLS = {
    "ENC_TYPE_NM", "ORDER_CLASS_NM", "DISPLAY_NAME", "MEDICATION_NM", "ORDER_STATUS_NM",
    "RECORD_TYPE", "MAR_ACTION_NM", "DOSE_UNIT_NM", "MED_ROUTE_NM",
}
_PATIENT_MEDICATIONS_SELECT_COLS = ", ".join(
    f"trim({c}) AS {c}" if c in _PATIENT_MEDICATIONS_TRIM_COLS else c
    for c in _PATIENT_MEDICATIONS_COLS
)
_PATIENT_MEDICATIONS_SQL = """
SELECT {cols}, count(*) AS n_occurrences
FROM (
  SELECT {select_cols}
  FROM read_parquet({{files}})
)
GROUP BY {cols}
""".format(cols=", ".join(_PATIENT_MEDICATIONS_COLS), select_cols=_PATIENT_MEDICATIONS_SELECT_COLS)


def build_patient_medications(catalog):
    files = bronze_files(catalog, "patient_medications")
    con = duckdb.connect()
    df = con.execute(_PATIENT_MEDICATIONS_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    n_expected = 27773144  # unchanged by trim pass -- verified 0 real merges (see above)
    if len(df) != n_expected:
        raise AssertionError(
            f"patient_medications: expected {n_expected:,} distinct rows, got {len(df):,} "
            "-- dedup logic no longer matches the validated shape, stopping before write."
        )
    log.info(f"[patient_medications] {len(df):,} rows (from {n_bronze:,} bronze)")

    write_silver_table(catalog, "patient_medications", df)


# --- patient_post_op_complications --------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "Silver-layer design checklist" ->
# patient_post_op_complications): 79% of bronze rows duplicated, 98% of that is the
# generic `AN AQI POST-OP COMPLICATIONS` reporting flag (SMRTDTA_ELEM_VALUE always null,
# up to 49x per encounter) -- same re-emission-per-note mechanism confirmed for
# patient_visit, despite this table having LOG_ID. A small remainder of real, non-null
# complication values (21 groups, 42 rows) duplicate the same way, grep-verified real.
# Collapsed to one row per group, repeat count kept as n_occurrences -- unlike
# patient_medications there is no dose/quantity column at risk here.
# Trim pass (2026-08-27, global standardization): trimmed all 4 categorical string
# columns (LOG_ID/MRN left untouched). Checked impact first: 0 merges -- purely cosmetic.
_PATIENT_POST_OP_COMPLICATIONS_COLS = [
    "LOG_ID", "MRN", "Element_Name", "CONTEXT_NAME", "Element_abbr", "SMRTDTA_ELEM_VALUE",
]
_PATIENT_POST_OP_COMPLICATIONS_TRIM_COLS = {
    "Element_Name", "CONTEXT_NAME", "Element_abbr", "SMRTDTA_ELEM_VALUE",
}
_PATIENT_POST_OP_COMPLICATIONS_SELECT_COLS = ", ".join(
    f"trim({c}) AS {c}" if c in _PATIENT_POST_OP_COMPLICATIONS_TRIM_COLS else c
    for c in _PATIENT_POST_OP_COMPLICATIONS_COLS
)
_PATIENT_POST_OP_COMPLICATIONS_SQL = """
SELECT {cols}, count(*) AS n_occurrences
FROM (
  SELECT {select_cols}
  FROM read_parquet({{files}})
)
GROUP BY {cols}
""".format(cols=", ".join(_PATIENT_POST_OP_COMPLICATIONS_COLS),
           select_cols=_PATIENT_POST_OP_COMPLICATIONS_SELECT_COLS)


def build_patient_post_op_complications(catalog):
    files = bronze_files(catalog, "patient_post_op_complications")
    con = duckdb.connect()
    df = con.execute(_PATIENT_POST_OP_COMPLICATIONS_SQL.format(files=files)).fetchdf()
    df["_silver_built_at"] = datetime.now(timezone.utc)

    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    n_expected = 84776  # distinct group count, verified by hand
    if len(df) != n_expected:
        raise AssertionError(
            f"patient_post_op_complications: expected {n_expected:,} distinct groups, "
            f"got {len(df):,} -- dedup logic no longer matches the validated shape, "
            "stopping before write."
        )
    log.info(f"[patient_post_op_complications] {len(df):,} rows (from {n_bronze:,} "
             f"bronze), max n_occurrences={df['n_occurrences'].max()}")

    write_silver_table(catalog, "patient_post_op_complications", df)


# --- flowsheets ----------------------------------------------------------------------------
# Dedup rule (see DATA_DICTIONARY.md "Duplicate-row audit" -> flowsheets): 63.6% of bronze
# rows (916,048,965 of 1,440,918,933) are exact duplicates on the full 9-column tuple -- by
# far the highest rate in the warehouse. Mechanism still unconfirmed (export re-emission vs.
# device-feed retry), but collapsing is recommended regardless of root cause: a genuinely
# distinct reading landing on a fully identical tuple by chance is implausible at
# minute-granularity/this scale, and the observed 323x outlier group already rules out
# coincidence. Bundled into the same pass (one 1.44B-row scan instead of two): trim
# FLO_NAME (the "Vital Signs " trailing-space duplicate alone is 27.7% of the whole table),
# normalize UNITS casing/typo variants (cmH20->cmH2O, l/min->L/min, ml->mL), and null the
# 6-row CSV-escaping corruption (an unescaped quote in a free-text note spilled garbage into
# UNITS) -- detected here as any UNITS value containing a comma or quote, which a real unit
# never does. RECORD_TYPE's 12.3% null rate and the 0.26% rows with neither
# MEAS_VALUE_NUM/TXT populated are both confirmed normal EHR sparse-grid behavior, not
# defects -- left untouched. FLO_DISPLAY_NAME's many-to-many relationship with FLO_NAME is
# by design (different care-unit templates reusing the same measurement names) -- also
# untouched. At ~1.44B bronze rows this cannot be materialized in pandas like every other
# table here -- written via write_silver_table_streaming instead of write_silver_table.
_FLOWSHEETS_SQL = """
WITH normalized AS (
  SELECT LOG_ID, MRN, trim(FLO_NAME) AS FLO_NAME, FLO_DISPLAY_NAME, RECORD_TYPE,
         RECORDED_TIME, MEAS_VALUE_NUM, MEAS_VALUE_TXT,
         CASE
           WHEN UNITS IS NULL THEN NULL
           WHEN UNITS LIKE '%,%' OR UNITS LIKE '%"%' THEN NULL
           WHEN trim(UNITS) = 'cmH20' THEN 'cmH2O'
           WHEN trim(UNITS) = 'l/min' THEN 'L/min'
           WHEN trim(UNITS) = 'ml' THEN 'mL'
           ELSE trim(UNITS)
         END AS UNITS
  FROM read_parquet({files})
)
SELECT LOG_ID, MRN, FLO_NAME, FLO_DISPLAY_NAME, RECORD_TYPE, RECORDED_TIME,
       MEAS_VALUE_NUM, MEAS_VALUE_TXT, UNITS, count(*) AS n_occurrences
FROM normalized
GROUP BY LOG_ID, MRN, FLO_NAME, FLO_DISPLAY_NAME, RECORD_TYPE, RECORDED_TIME,
         MEAS_VALUE_NUM, MEAS_VALUE_TXT, UNITS
"""


def build_flowsheets(catalog):
    files = bronze_files(catalog, "flowsheets")
    con = duckdb.connect()
    n_bronze = con.execute(f"SELECT count(*) FROM read_parquet({files})").fetchone()[0]
    log.info(f"[flowsheets] bronze rows: {n_bronze:,} -- streaming dedup+write, this will take a while")

    n_written = write_silver_table_streaming(catalog, "flowsheets", _FLOWSHEETS_SQL, files)

    # Cheap post-write check instead of re-scanning bronze: sum(n_occurrences) must equal
    # the bronze row count exactly -- guaranteed by GROUP BY semantics as long as no row
    # was silently dropped or double-counted across the streaming batches.
    table = catalog.load_table("silver.flowsheets")
    sfiles = "[" + ",".join(f"'{t.file.file_path}'" for t in table.scan().plan_files()) + "]"
    n_sum = con.execute(f"SELECT sum(n_occurrences) FROM read_parquet({sfiles})").fetchone()[0]
    if int(n_sum) != n_bronze:
        raise AssertionError(
            f"flowsheets: sum(n_occurrences)={int(n_sum):,} != bronze rows {n_bronze:,} "
            "-- some rows were lost or double-counted during the streaming write."
        )
    log.info(f"[flowsheets] verified: {n_written:,} silver rows, sum(n_occurrences)={int(n_sum):,} "
             f"matches bronze exactly")


BUILDERS = {
    "patient_information": build_patient_information,
    "patient_lda": build_patient_lda,
    "patient_history": build_patient_history,
    "patient_visit": build_patient_visit,
    "patient_coding": build_patient_coding,
    "patient_medications": build_patient_medications,
    "patient_post_op_complications": build_patient_post_op_complications,
    "flowsheets": build_flowsheets,
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
