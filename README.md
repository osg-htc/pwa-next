# pwa-next

A replacement for perfSONAR's pSConfig Web Admin: define tests, host groups and meshes in a browser, and publish them as pSConfig templates. See [DESIGN.md](DESIGN.md) for what it does and why it is built this way, and [docs/MIGRATION.md](docs/MIGRATION.md) for moving an existing PWA over.

Status: working prototype. Not deployed, not packaged.

## Try it

Needs Python 3.9 or later.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q

export PWA_CONFIG=$PWD/dev.env          # development settings: SQLite, pick-a-user sign-in
.venv/bin/python -m pwa init-db
.venv/bin/python -m pwa serve           # http://127.0.0.1:8085
```

`dev.env` uses `PWA_AUTH=dev`, which lets anyone sign in as anyone. It is for a loopback address only.

## Commands

```
python -m pwa init-db                   create the tables
python -m pwa import-pwa <export-dir>   import a PWA database export, then publish
python -m pwa lookup-sync [--dry-run]   refresh hosts from the Lookup Service, then publish
python -m pwa publish                   publish everything again
python -m pwa serve [--port N]          development server
```

Settings are environment variables, or `KEY=value` lines in the file named by `PWA_CONFIG`; `pwa/config.py` lists them.

## Bundled third-party files

- `pwa/specs/tests/*.json`, `pwa/specs/archivers/*.json`: `spec-jsonschema.json` files from perfsonar/pscheduler, branch 5.3.0 (commit a66acdc6), Apache License 2.0.
- `pwa/specs/psconfig-schema.json`: from perfsonar/psconfig v5.2.6, Apache License 2.0.
- `pwa/web/static/htmx.min.js`: htmx 1.9.12, BSD 2-Clause (sha256 449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452).
