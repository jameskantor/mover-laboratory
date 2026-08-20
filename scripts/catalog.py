from pathlib import Path
from pyiceberg.catalog.sql import SqlCatalog

# Data written under this path (via the /D:/... symlink created by entrypoint.sh) ends
# up with baked-in Iceberg manifest paths that resolve correctly both inside the
# container (/work/...) and natively on Windows (D:/Data_Science_Projects/Mover/...,
# once DuckDB strips the single leading "/" it always strips on Windows). See
# entrypoint.sh and DATA_DICTIONARY.md for the full explanation.
WAREHOUSE_DIR = Path("/D:/Data_Science_Projects/Mover/iceberg_warehouse")
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
