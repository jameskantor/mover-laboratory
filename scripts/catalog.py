import os
from pathlib import Path
from pyiceberg.catalog.sql import SqlCatalog

# Portable by default: the warehouse lives under /work (wherever the caller bind-mounts
# their own data), with no assumption about host OS or folder layout, so this works
# unmodified for anyone who clones the repo and points it at their own MOVER download.
#
# Set MOVER_WAREHOUSE_DIR to opt into the alternate mode this project actually runs in on
# this machine: a path like /D:/Data_Science_Projects/Mover/iceberg_warehouse, paired with
# entrypoint.sh symlinking that path to /work (via WINDOWS_HOST_PATH). Iceberg bakes
# absolute warehouse paths into every manifest/snapshot file at write time, and native
# Windows DuckDB (e.g. DBeaver) strips exactly one leading "/" and resolves the rest
# relative to CWD -- so a warehouse path that isn't already a Windows-drive-letter path
# once that slash is stripped is unreadable from Windows tools, regardless of mounts or
# junctions. See docs/DATA_DICTIONARY.md for the full history.
WAREHOUSE_DIR = Path(os.environ.get("MOVER_WAREHOUSE_DIR", "/work/iceberg_warehouse"))
# The catalog's own SQLite file doesn't get baked into any manifest, so it's fine (and
# simpler) to keep it on the plain /work path rather than through the colon-path symlink.
CATALOG_DB = Path("/work/iceberg_warehouse/catalog.db")


def get_catalog():
    WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_DB.parent.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog(
        "mover",
        **{
            "uri": f"sqlite:///{CATALOG_DB}",
            "warehouse": f"file://{WAREHOUSE_DIR}",
        },
    )
    if "bronze" not in [ns[0] for ns in catalog.list_namespaces()]:
        catalog.create_namespace("bronze")
    return catalog
