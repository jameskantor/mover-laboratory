#!/bin/sh
# Optional: set WINDOWS_HOST_PATH (e.g. "D:/Data_Science_Projects/Mover") to make that
# exact path resolve to /work inside the container. Only needed alongside
# MOVER_WAREHOUSE_DIR when a warehouse under that path must stay readable from native
# Windows DuckDB (e.g. DBeaver) -- see catalog.py for why. Portable setups that don't need
# native-Windows access can skip this entirely; no symlink is created unless it's set.
if [ -n "$WINDOWS_HOST_PATH" ]; then
    parent_dir=$(dirname "/$WINDOWS_HOST_PATH")
    mkdir -p "$parent_dir"
    [ -L "/$WINDOWS_HOST_PATH" ] || ln -s /work "/$WINDOWS_HOST_PATH"
fi
exec "$@"
