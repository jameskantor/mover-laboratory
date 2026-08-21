# One-time setup: creates a persistent, named container that survives terminal close,
# Docker restarts, and (once Docker Desktop's own "Start Docker Desktop when you sign in"
# setting is also on) Windows sign-in. Unlike jupyter.ps1 (--rm -it, disposable, attached to
# whatever terminal ran it), this one runs detached with --restart unless-stopped, so Docker
# itself brings it back up any time the Docker daemon starts -- no Windows Scheduled Task
# needed. Re-running this script is safe: it replaces any existing mover-lab container.
# Usage: .\autostart.ps1
docker rm -f mover-lab 2>$null | Out-Null

docker run -d --name mover-lab --restart unless-stopped `
    -p 127.0.0.1:8888:8888 -p 9494:9494 -v "${PWD}:/work" `
    -v mover-warehouse:/work/iceberg_warehouse `
    -e WINDOWS_HOST_PATH=D:/Data_Science_Projects/Mover `
    -e MOVER_WAREHOUSE_DIR=/D:/Data_Science_Projects/Mover/iceberg_warehouse `
    mover-laboratory:latest sh /work/scripts/start_lab.sh

Write-Host "mover-lab is running detached (restart policy: unless-stopped)."
Write-Host "Logs:  docker logs -f mover-lab"
Write-Host "Stop:  docker stop mover-lab   (won't restart until you 'docker start mover-lab' again)"
