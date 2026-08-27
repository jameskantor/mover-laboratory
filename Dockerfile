FROM python:3.12-slim

RUN pip install --no-cache-dir \
    "pyiceberg[sql-sqlite,pyarrow]" \
    duckdb \
    pandas \
    pyarrow \
    "dask[dataframe]" \
    jupyterlab \
    matplotlib \
    seaborn \
    pytest

# DuckDB's UI extension shells out to xdg-open to launch a browser; this image has no
# browser and no xdg-open, so it crashes with an unhandled exception. Stub it out as a
# no-op so start_ui() just starts the server without trying to auto-open anything.
RUN printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/xdg-open && chmod +x /usr/local/bin/xdg-open

WORKDIR /work
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Default: launch the lab (JupyterLab + Quack server, see scripts/start_lab.sh) when no
# command is given. query.ps1 and ingestion pass their own explicit command and override
# this either way -- this only matters for a bare `docker run` with no args, so a new user
# gets a working lab without needing to know jupyter.ps1's exact invocation first. Assumes
# the project is mounted at /work, same as everything else in this image.
EXPOSE 8888 9494
CMD ["sh", "/work/scripts/start_lab.sh"]
