FROM python:3.12-slim

RUN pip install --no-cache-dir \
    "pyiceberg[sql-sqlite,pyarrow]" \
    duckdb \
    pandas \
    pyarrow \
    "dask[dataframe]" \
    jupyterlab \
    matplotlib \
    seaborn

# DuckDB's UI extension shells out to xdg-open to launch a browser; this image has no
# browser and no xdg-open, so it crashes with an unhandled exception. Stub it out as a
# no-op so start_ui() just starts the server without trying to auto-open anything.
RUN printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/xdg-open && chmod +x /usr/local/bin/xdg-open

WORKDIR /work
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
