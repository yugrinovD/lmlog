#!/usr/bin/env bash
# Reproducible terminal demo used for docs/demo.gif.
set -euo pipefail

LMLOG="${LMLOG:-lmlog}"
DATA="${DATA:-/tmp/lmlog-demo-data}"

rm -rf "$DATA"
mkdir -p "$DATA/logs"
clear

printf '$ lmlog generate --lines 20000 --days 1 --profile demo\n'
"$LMLOG" generate --lines 20000 --days 1 --profile demo --out "$DATA/logs" >/dev/null
sleep 1

printf '\n$ lmlog ingest --log-dir "$DATA/logs" --db "$DATA/lmlog.db"\n'
"$LMLOG" ingest --log-dir "$DATA/logs" --db "$DATA/lmlog.db"
sleep 2

printf '\n$ lmlog errors -n 3 --db "$DATA/lmlog.db"\n'
"$LMLOG" errors -n 3 --db "$DATA/lmlog.db"
sleep 2

printf '\n$ lmlog trends -n 5 --db "$DATA/lmlog.db"\n'
"$LMLOG" trends -n 5 --db "$DATA/lmlog.db"
sleep 2

printf '\n$ lmlog explain -n 2 --no-llm --db "$DATA/lmlog.db"\n'
"$LMLOG" explain -n 2 --no-llm --db "$DATA/lmlog.db"
sleep 3
