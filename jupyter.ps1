# Launches JupyterLab against the project, with all bronze tables queryable via DuckDB.
# Usage: .\jupyter.ps1
# Then open the URL it prints (http://localhost:8888/...) in your browser. Ctrl+C to stop.
# Warehouse data lives in the mover-warehouse Docker-native volume -- see query.ps1 /
# docs/BUILD_LOG.md for why the WINDOWS_HOST_PATH/MOVER_WAREHOUSE_DIR env vars are still here.
docker run --rm -it -p 127.0.0.1:8888:8888 -v "${PWD}:/work" `
    -v mover-warehouse:/work/iceberg_warehouse `
    -e WINDOWS_HOST_PATH=D:/Data_Science_Projects/Mover `
    -e MOVER_WAREHOUSE_DIR=/D:/Data_Science_Projects/Mover/iceberg_warehouse `
    mover-laboratory:latest `
    jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --NotebookApp.token='' --NotebookApp.password=''
