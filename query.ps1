# Launches an interactive query shell against the bronze Iceberg tables.
# Usage: .\query.ps1
docker run --rm -it -v "${PWD}:/work" mover-iceberg:latest python -i /work/scripts/shell.py
