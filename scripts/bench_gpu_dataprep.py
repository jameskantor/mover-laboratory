"""
GPU data-prep benchmark for docs/TECH_EVALUATIONS.md's "GPU-accelerated dataframe prep"
entry. Compares three engines on the same real operation: per-(LOG_ID, FLO_DISPLAY_NAME)
summary stats (count/mean/min/max/last-by-time) for core vitals (Resp/Pulse/SpO2/Temp)
over one year-partition of bronze.flowsheets.

Run once per engine (separate process = clean memory state each time):
    python scripts/bench_gpu_dataprep.py --engine duckdb
    python scripts/bench_gpu_dataprep.py --engine cudf
    python scripts/bench_gpu_dataprep.py --engine cudfpandas
"""
import argparse
import os
import resource
import subprocess
import sys
import time

sys.path.insert(0, "/work/scripts")

VITALS = ["Resp", "Pulse", "SpO2", "Temp"]
YEAR = 2022


def resolve_files():
    # flowsheets is Iceberg-partitioned by year only, so a month filter can't prune at
    # the file level here — all of YEAR's files are always returned. Month scoping for a
    # smaller first test happens as a row filter inside run_*() instead, see _month_bounds().
    from catalog import get_catalog
    from pyiceberg.expressions import And, GreaterThanOrEqual, LessThan

    catalog = get_catalog()
    table = catalog.load_table("bronze.flowsheets")
    scan = table.scan(
        row_filter=And(
            GreaterThanOrEqual("RECORDED_TIME", f"{YEAR}-01-01T00:00:00"),
            LessThan("RECORDED_TIME", f"{YEAR + 1}-01-01T00:00:00"),
        )
    )
    files = [t.file.file_path for t in scan.plan_files()]
    return files


def gpu_mem_used_mb():
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True,
    )
    return int(out.stdout.strip().splitlines()[0])


def _month_bounds(month):
    if month is None:
        return f"{YEAR}-01-01T00:00:00", f"{YEAR + 1}-01-01T00:00:00"
    next_month, next_year = (month + 1, YEAR) if month < 12 else (1, YEAR + 1)
    return f"{YEAR}-{month:02d}-01T00:00:00", f"{next_year}-{next_month:02d}-01T00:00:00"


def run_duckdb(files, month=None):
    import duckdb
    con = duckdb.connect()
    file_list = "[" + ",".join(f"'{f}'" for f in files) + "]"
    vitals_list = ",".join(f"'{v}'" for v in VITALS)
    lo, hi = _month_bounds(month)
    q = f"""
        SELECT LOG_ID, FLO_DISPLAY_NAME,
               count(MEAS_VALUE_NUM) AS n,
               avg(MEAS_VALUE_NUM) AS mean,
               min(MEAS_VALUE_NUM) AS min,
               max(MEAS_VALUE_NUM) AS max,
               arg_max(MEAS_VALUE_NUM, RECORDED_TIME) AS last
        FROM read_parquet({file_list})
        WHERE FLO_DISPLAY_NAME IN ({vitals_list}) AND MEAS_VALUE_NUM IS NOT NULL
          AND RECORDED_TIME >= '{lo}' AND RECORDED_TIME < '{hi}'
        GROUP BY LOG_ID, FLO_DISPLAY_NAME
    """
    result = con.execute(q).fetch_arrow_table()
    return result.num_rows


def run_cudf(files, month=None):
    import cudf  # already warmed up in main(); this is a cheap cached re-import
    lo, hi = _month_bounds(month)
    cols = ["LOG_ID", "FLO_DISPLAY_NAME", "MEAS_VALUE_NUM", "RECORDED_TIME"]
    df = cudf.read_parquet(files, columns=cols)
    df = df[
        df["FLO_DISPLAY_NAME"].isin(VITALS) & df["MEAS_VALUE_NUM"].notna()
        & (df["RECORDED_TIME"] >= lo) & (df["RECORDED_TIME"] < hi)
    ]
    g = df.groupby(["LOG_ID", "FLO_DISPLAY_NAME"])["MEAS_VALUE_NUM"].agg(
        ["count", "mean", "min", "max"]
    )
    df_sorted = df.sort_values("RECORDED_TIME")
    last = df_sorted.groupby(["LOG_ID", "FLO_DISPLAY_NAME"])["MEAS_VALUE_NUM"].agg("last")
    return len(g), len(last)


def run_cudfpandas(files, month=None):
    import pandas as pd  # already installed/warmed up in main(); cheap cached re-import
    lo, hi = _month_bounds(month)
    cols = ["LOG_ID", "FLO_DISPLAY_NAME", "MEAS_VALUE_NUM", "RECORDED_TIME"]
    df = pd.read_parquet(files, columns=cols)
    df = df[
        df["FLO_DISPLAY_NAME"].isin(VITALS) & df["MEAS_VALUE_NUM"].notna()
        & (df["RECORDED_TIME"] >= lo) & (df["RECORDED_TIME"] < hi)
    ]
    g = df.groupby(["LOG_ID", "FLO_DISPLAY_NAME"])["MEAS_VALUE_NUM"].agg(
        ["count", "mean", "min", "max"]
    )
    df_sorted = df.sort_values("RECORDED_TIME")
    last = df_sorted.groupby(["LOG_ID", "FLO_DISPLAY_NAME"])["MEAS_VALUE_NUM"].agg("last")
    return len(g), len(last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=["duckdb", "cudf", "cudfpandas"])
    ap.add_argument("--month", type=int, choices=range(1, 13), default=None,
                     help="Scope to one month of YEAR instead of the full year (row filter, "
                          "not file pruning — flowsheets is only partitioned by year).")
    args = ap.parse_args()

    # Import (and pay any CUDA context / JIT warmup cost) before timing starts, so the
    # comparison is apples-to-apples on the actual operation, not first-import overhead.
    if args.engine == "duckdb":
        import duckdb  # noqa: F401
    elif args.engine == "cudf":
        import cudf  # noqa: F401
    else:
        import cudf.pandas
        cudf.pandas.install()
        import pandas  # noqa: F401

    t0 = time.perf_counter()
    files = resolve_files()
    t_resolve = time.perf_counter() - t0
    scope = f"{YEAR}-{args.month:02d}" if args.month else str(YEAR)
    print(f"[{args.engine}] resolved {len(files)} files, scoped to {scope}, in {t_resolve:.2f}s", flush=True)

    gpu_before = gpu_mem_used_mb() if args.engine != "duckdb" else None

    t0 = time.perf_counter()
    if args.engine == "duckdb":
        rows = run_duckdb(files, args.month)
    elif args.engine == "cudf":
        rows = run_cudf(files, args.month)
    else:
        rows = run_cudfpandas(files, args.month)
    elapsed = time.perf_counter() - t0

    ru = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss_mb = ru.ru_maxrss / 1024
    gpu_after = gpu_mem_used_mb() if args.engine != "duckdb" else None

    print(f"[{args.engine}] result rows: {rows}")
    print(f"[{args.engine}] wall clock: {elapsed:.3f}s")
    print(f"[{args.engine}] peak CPU RSS: {peak_rss_mb:.0f} MB")
    if gpu_before is not None:
        print(f"[{args.engine}] GPU mem used: before={gpu_before}MB after={gpu_after}MB delta={gpu_after - gpu_before}MB")


if __name__ == "__main__":
    main()
