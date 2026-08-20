# Launches an interactive query shell against the bronze Iceberg tables.
# Usage: .\query.ps1
# Warehouse data lives in the mover-warehouse Docker-native volume, not a Windows bind
# mount -- mounted at /work/iceberg_warehouse, with WINDOWS_HOST_PATH/MOVER_WAREHOUSE_DIR
# reused so the existing baked-in manifest paths (still shaped like a Windows path from
# the original ingestion) resolve through the symlink into the volume. See BUILD_LOG.md.
docker run --rm -it -v "${PWD}:/work" `
    -v mover-warehouse:/work/iceberg_warehouse `
    -e WINDOWS_HOST_PATH=D:/Data_Science_Projects/Mover `
    -e MOVER_WAREHOUSE_DIR=/D:/Data_Science_Projects/Mover/iceberg_warehouse `
    mover-laboratory:latest python -i /work/scripts/shell.py
