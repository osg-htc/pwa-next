#!/bin/bash
# Goes on the PRODUCTION PWA host as /usr/local/sbin/pwa-export-for-shadow, mode 700 root.
# It is the only thing the shadow host's key may run (see README.md): it writes a
# tar.gz of PWA's five collections to standard output and changes nothing.
set -euo pipefail
mongo=$(docker ps --format '{{.Names}}' | grep -i mongo | head -1)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for c in hosts hostgroups testspecs archives configs; do
    docker exec "$mongo" mongoexport --quiet --db mca --collection "$c" --jsonArray > "$tmp/$c.json"
done
tar -cz -C "$tmp" .
