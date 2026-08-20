# Launches JupyterLab against the project, with all bronze tables queryable via DuckDB.
# Usage: .\jupyter.ps1
# Then open the URL it prints (http://localhost:8888/...) in your browser. Ctrl+C to stop.
docker run --rm -it -p 127.0.0.1:8888:8888 -v "${PWD}:/work" mover-iceberg:latest `
    jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --NotebookApp.token='' --NotebookApp.password=''
