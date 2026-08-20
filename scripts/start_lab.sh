#!/bin/sh
# Starts the Quack server in the background and JupyterLab in the foreground, in the same
# container -- one persistent container serving both roles, instead of a separate
# dedicated container for each. See docs/BUILD_LOG.md.
python /work/scripts/quack_server.py &
exec jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --NotebookApp.token='' --NotebookApp.password=''
