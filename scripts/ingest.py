import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).parent))
from catalog import get_catalog
from schemas import TABLES

DATA_DIR = Path("/work/EMR/EPIC_EMR")
STATUS_PATH = Path("/work/iceberg_warehouse/_ingestion_status.json")
LOG_DIR = Path("/work/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "ingest.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ingest")

CHUNKSIZE_SMALL = 500_000
CHUNKSIZE_LARGE = 2_000_000


def load_status():
    if STATUS_PATH.exists():
        return json.loads(STATUS_PATH.read_text())
    return {}


def save_status(status):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2, default=str))


def count_source_rows(csv_path: Path) -> int:
    out = subprocess.run(["wc", "-l", str(csv_path)], capture_output=True, text=True)
    return int(out.stdout.split()[0]) - 1  # minus header


def arrow_type_for(iceberg_field):
    from pyiceberg.types import StringType, LongType, DoubleType, TimestampType
    t = iceberg_field.field_type
    if isinstance(t, StringType):
        return pa.string()
    if isinstance(t, LongType):
        return pa.int64()
    if isinstance(t, DoubleType):
        return pa.float64()
    if isinstance(t, TimestampType):
        return pa.timestamp("us")
    raise ValueError(f"unhandled type {t}")


def clean_chunk(df: pd.DataFrame, table_key: str, conf: dict, source_file: str) -> pa.Table:
    from pyiceberg.types import LongType, DoubleType, TimestampType

    if "column_rename" in conf:
        df = df.rename(columns=conf["column_rename"])
    if "drop_cols" in conf:
        df = df.drop(columns=[c for c in conf["drop_cols"] if c in df.columns], errors="ignore")

    if table_key == "flowsheets":
        numeric = pd.to_numeric(df["MEAS_VALUE"], errors="coerce")
        df["MEAS_VALUE_NUM"] = numeric
        df["MEAS_VALUE_TXT"] = df["MEAS_VALUE"].where(numeric.isna())
        df = df.drop(columns=["MEAS_VALUE"])

    # Coerce every non-string column per its declared schema type, rather than relying on
    # manually-maintained column lists (which previously missed DoubleType columns like
    # ADMIN_SIG, causing a crash on a malformed value like "0 NULL").
    schema = conf["schema"]
    for f in schema.fields:
        if f.name not in df.columns:
            continue
        if isinstance(f.field_type, TimestampType):
            df[f.name] = pd.to_datetime(df[f.name], errors="coerce")
        elif isinstance(f.field_type, LongType):
            df[f.name] = pd.to_numeric(df[f.name], errors="coerce").astype("Int64")
        elif isinstance(f.field_type, DoubleType):
            df[f.name] = pd.to_numeric(df[f.name], errors="coerce")

    df["_source_file"] = source_file
    df["_ingested_at"] = datetime.now(timezone.utc)

    ordered_cols = [f.name for f in schema.fields]
    for col in ordered_cols:
        if col not in df.columns:
            df[col] = None
    df = df[ordered_cols]

    pa_fields = [pa.field(f.name, arrow_type_for(f), nullable=not f.required) for f in schema.fields]
    pa_schema = pa.schema(pa_fields)
    return pa.Table.from_pandas(df, schema=pa_schema, preserve_index=False)


def ensure_table(catalog, table_key, conf):
    identifier = f"bronze.{table_key}"
    try:
        return catalog.load_table(identifier)
    except Exception:
        kwargs = {"schema": conf["schema"]}
        if conf.get("partition_spec") is not None:
            kwargs["partition_spec"] = conf["partition_spec"]
        return catalog.create_table(identifier, **kwargs)


def purge_source_file(table, source_file: str):
    """Delete any rows already committed for this source file, so re-ingesting after a
    mid-file crash can't duplicate the rows written before the crash. Makes ingestion of
    any single source file idempotent regardless of where a prior attempt failed."""
    from pyiceberg.expressions import EqualTo
    try:
        table.delete(delete_filter=EqualTo("_source_file", source_file))
    except Exception:
        log.exception(f"purge_source_file failed for {source_file} (continuing anyway — "
                       f"table may not have had any matching rows yet)")


def ingest_simple_table(catalog, table_key, conf, status):
    if status.get(table_key, {}).get("status") == "done":
        log.info(f"[{table_key}] already done, skipping")
        return

    csv_path = DATA_DIR / conf["source_csv"]
    log.info(f"[{table_key}] starting, source={csv_path}")
    source_rows = count_source_rows(csv_path)
    log.info(f"[{table_key}] source row count = {source_rows:,}")

    table = ensure_table(catalog, table_key, conf)
    purge_source_file(table, conf["source_csv"])
    ingested = 0
    for chunk in pd.read_csv(csv_path, chunksize=CHUNKSIZE_SMALL, dtype=str, low_memory=False):
        arrow_tbl = clean_chunk(chunk, table_key, conf, conf["source_csv"])
        table.append(arrow_tbl)
        ingested += len(chunk)
        log.info(f"[{table_key}] ingested {ingested:,}/{source_rows:,}")

    status[table_key] = {
        "status": "done", "source_rows": source_rows, "ingested_rows": ingested,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "row_match": ingested == source_rows,
    }
    save_status(status)
    log.info(f"[{table_key}] DONE. ingested={ingested:,} source={source_rows:,} match={ingested == source_rows}")


def ingest_flowsheets(catalog, conf, status):
    flow_dir = DATA_DIR / conf["source_dir"]
    parts = sorted(flow_dir.glob("flowsheet_part*.csv"),
                    key=lambda p: int("".join(filter(str.isdigit, p.stem))))

    table = ensure_table(catalog, "flowsheets", conf)
    fs_status = status.setdefault("flowsheets", {"status": "in_progress", "parts_done": {}})

    for part_path in parts:
        part_name = part_path.name
        if fs_status["parts_done"].get(part_name, {}).get("status") == "done":
            log.info(f"[flowsheets] {part_name} already done, skipping")
            continue

        log.info(f"[flowsheets] starting {part_name}")
        source_rows = count_source_rows(part_path)
        log.info(f"[flowsheets] {part_name} source row count = {source_rows:,}")
        purge_source_file(table, part_name)

        ingested = 0
        for chunk in pd.read_csv(part_path, chunksize=CHUNKSIZE_LARGE, dtype=str, low_memory=False):
            arrow_tbl = clean_chunk(chunk, "flowsheets", conf, part_name)
            table.append(arrow_tbl)
            ingested += len(chunk)
            log.info(f"[flowsheets] {part_name}: {ingested:,}/{source_rows:,}")

        fs_status["parts_done"][part_name] = {
            "status": "done", "source_rows": source_rows, "ingested_rows": ingested,
            "row_match": ingested == source_rows,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        save_status(status)
        log.info(f"[flowsheets] {part_name} DONE. ingested={ingested:,} source={source_rows:,} "
                 f"match={ingested == source_rows}")

    all_done = all(v.get("status") == "done" for v in fs_status["parts_done"].values()) \
        and len(fs_status["parts_done"]) == len(parts)
    fs_status["status"] = "done" if all_done else "incomplete"
    save_status(status)
    log.info(f"[flowsheets] ALL PARTS {'DONE' if all_done else 'INCOMPLETE'}")


def main():
    log.info("=== ingestion run starting ===")
    catalog = get_catalog()
    status = load_status()

    simple_order = [
        "patient_information", "patient_history", "patient_visit", "patient_coding",
        "patient_post_op_complications", "patient_lda", "patient_procedure_events",
        "patient_labs", "patient_medications",
    ]
    for table_key in simple_order:
        try:
            ingest_simple_table(catalog, table_key, TABLES[table_key], status)
        except Exception:
            log.exception(f"[{table_key}] FAILED")
            raise

    try:
        ingest_flowsheets(catalog, TABLES["flowsheets"], status)
    except Exception:
        log.exception("[flowsheets] FAILED")
        raise

    log.info("=== ingestion run complete ===")


if __name__ == "__main__":
    main()
