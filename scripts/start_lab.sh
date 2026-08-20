#!/bin/sh
# Starts the Quack server in the background and JupyterLab in the foreground, in the same
# container -- one persistent container serving both roles, instead of a separate
# dedicated container for each. See docs/BUILD_LOG.md.
python /work/scripts/quack_server.py &
# disable_check_xsrf: required for VS Code's "Existing Jupyter Server" remote-kernel
# connection -- its non-browser client doesn't do the XSRF cookie handshake a browser tab
# does, so session-creation POSTs get rejected with 403 '_xsrf' argument missing, and cells
# just hang queued forever. Safe here since there's no auth anyway (loopback + LAN dev use
# only, see docs/BUILD_LOG.md).
exec jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root \
    --ServerApp.token='' --ServerApp.password='' --ServerApp.disable_check_xsrf=True
