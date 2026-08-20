# Serves the bronze Iceberg tables over DuckDB's Quack remote protocol (beta), so
# Windows-side tools can query them over the network without reading files directly.
# Usage: .\quack.ps1
# Native DuckDB CLI test from another terminal:
#   ATTACH 'quack:localhost:9494?token=mover-lab-dev-token' AS bronze (TYPE quack);
#   SELECT COUNT(*) FROM bronze.flowsheets;
# Ctrl+C to stop.
docker run --rm -it -p 9494:9494 -v "${PWD}:/work" `
    -v mover-warehouse:/work/iceberg_warehouse `
    -e WINDOWS_HOST_PATH=D:/Data_Science_Projects/Mover `
    -e MOVER_WAREHOUSE_DIR=/D:/Data_Science_Projects/Mover/iceberg_warehouse `
    mover-laboratory:latest python /work/scripts/quack_server.py
