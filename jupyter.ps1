# Launches JupyterLab AND the Quack server together in one container -- notebook at
# localhost:8888, bronze tables also queryable over the network (DBeaver, native DuckDB
# CLI) at localhost:9494. One persistent container serving both roles. Ctrl+C stops both.
# Usage: .\jupyter.ps1
# Warehouse data lives in the mover-warehouse Docker-native volume -- see query.ps1 /
# docs/BUILD_LOG.md for why the WINDOWS_HOST_PATH/MOVER_WAREHOUSE_DIR env vars are still here.
docker run --rm -it -p 127.0.0.1:8888:8888 -p 9494:9494 -v "${PWD}:/work" `
    -v mover-warehouse:/work/iceberg_warehouse `
    -e WINDOWS_HOST_PATH=D:/Data_Science_Projects/Mover `
    -e MOVER_WAREHOUSE_DIR=/D:/Data_Science_Projects/Mover/iceberg_warehouse `
    mover-laboratory:latest sh /work/scripts/start_lab.sh
