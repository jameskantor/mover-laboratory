#!/bin/sh
# Iceberg bakes absolute warehouse paths into its manifest/snapshot files at write time.
# DuckDB on native Windows only strips a single leading "/" and does not resolve
# CWD-relative or drive-relative POSIX paths beyond that -- so any warehouse path that
# doesn't already look like a Windows drive-letter path once that one slash is gone will
# break when the same Iceberg tables are read from Windows (e.g. via DBeaver).
#
# Fix: make the warehouse path be exactly "/D:/Data_Science_Projects/Mover/..." inside
# the container (via this symlink), so every baked-in reference is already a valid
# Windows path once DuckDB strips the leading slash on read.
mkdir -p "/D:/Data_Science_Projects"
[ -L "/D:/Data_Science_Projects/Mover" ] || ln -s /work "/D:/Data_Science_Projects/Mover"
exec "$@"
