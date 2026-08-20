import pandas as pd
import subprocess
from pathlib import Path

DATA_DIR = Path("/work/EMR/EPIC_EMR")

def audit_csv(path, nrows=200_000):
    print(f"\n{'='*70}\n{path.relative_to(DATA_DIR)}")
    df = pd.read_csv(path, nrows=nrows, low_memory=False)
    print(f"  columns ({len(df.columns)}): {list(df.columns)}")
    print(f"  dtypes (sample of {len(df)} rows):")
    for col in df.columns:
        n_null = df[col].isna().sum()
        n_unique = df[col].nunique()
        print(f"    {col!r:35s} {str(df[col].dtype):10s} nulls={n_null:>7d} unique={n_unique:>7d}")

for csv_file in sorted(DATA_DIR.glob("*.csv")):
    audit_csv(csv_file)

flow_dir = DATA_DIR / "flowsheets_cleaned"
print(f"\n{'='*70}\nFLOWSHEETS: row counts per part (wc -l equivalent, this may take a bit)")
for csv_file in sorted(flow_dir.glob("*.csv"), key=lambda p: int(''.join(filter(str.isdigit, p.stem)))):
    n = int(subprocess.run(["wc", "-l", str(csv_file)], capture_output=True, text=True).stdout.split()[0]) - 1  # minus header
    size_gb = csv_file.stat().st_size / 1e9
    print(f"  {csv_file.name:25s} rows={n:>12,d}  size={size_gb:6.2f}GB")

print(f"\n{'='*70}\nflowsheet_part1.csv sample (first 200k rows) dtype audit")
audit_csv(flow_dir / "flowsheet_part1.csv")

print(f"\n{'='*70}\nMEAS_VALUE mixed-type check across parts (sample 500k rows each of part1, part2)")
for pname in ["flowsheet_part1.csv", "flowsheet_part2.csv"]:
    df = pd.read_csv(flow_dir / pname, nrows=500_000, usecols=["MEAS_VALUE"], low_memory=False)
    numeric = pd.to_numeric(df["MEAS_VALUE"], errors="coerce")
    n_numeric = numeric.notna().sum()
    n_text = df["MEAS_VALUE"].notna().sum() - n_numeric
    print(f"  {pname}: numeric={n_numeric}, text={n_text}, null={df['MEAS_VALUE'].isna().sum()}")
