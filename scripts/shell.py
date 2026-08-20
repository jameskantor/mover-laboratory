"""Interactive query shell for the bronze Iceberg tables.

Run with: docker run --rm -it -v "/d/Data_Science_Projects/Mover:/work" mover-iceberg:latest python -i /work/scripts/shell.py
(or just use query.ps1 / query.sh from the project root)

Drops you into a Python REPL with `con` connected and every bronze table
registered as a plain SQL view, so you can just write:
    con.sql("SELECT * FROM patient_labs LIMIT 10").show()
    con.sql("SELECT COUNT(*) FROM flowsheets").show()
"""
import duckdb

WAREHOUSE = "/D:/Data_Science_Projects/Mover/iceberg_warehouse/bronze"
TABLES = [
    "patient_information", "patient_history", "patient_visit", "patient_coding",
    "patient_post_op_complications", "patient_lda", "patient_procedure_events",
    "patient_labs", "patient_medications", "flowsheets",
]

con = duckdb.connect()
con.execute("INSTALL iceberg; LOAD iceberg; SET unsafe_enable_version_guessing = true;")
for t in TABLES:
    con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM iceberg_scan('{WAREHOUSE}/{t}')")

print("Bronze tables ready as views:", ", ".join(TABLES))
print("Example: con.sql('SELECT * FROM patient_labs LIMIT 10').show()")
