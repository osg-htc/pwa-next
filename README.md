<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/lockup/horizontal-for-dark.svg">
    <img alt="pwa-next" src="docs/brand/lockup/horizontal-for-light.svg" width="420">
  </picture>
</p>

# pwa-next

A replacement for perfSONAR's pSConfig Web Admin: define tests, host groups and meshes in a browser, and publish them as pSConfig templates. See [DESIGN.md](DESIGN.md) for what it does and why it is built this way, and [docs/MIGRATION.md](docs/MIGRATION.md) for moving an existing PWA over.

Status: working prototype. Packaged as an EL9 RPM and rehearsed in containers; not deployed yet.

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

To run the tests against PostgreSQL instead of SQLite (every table in that database is dropped and re-created for each test, so give it a database of its own):

```
.venv/bin/pip install psycopg2-binary
PWA_TEST_DATABASE_URL=postgresql://USER:PASSWORD@HOST/pwa_test .venv/bin/python -m pytest -q
```

`dev.env` uses `PWA_AUTH=dev`, which lets anyone sign in as anyone. It is for a loopback address only.

## Packaging and deployment

- `packaging/rehearse.sh [export-dir]` builds the RPM in a clean EL9 container, installs it with EL9's own packages, runs the tests on SQLite and PostgreSQL, and checks it under Apache. The RPM lands in `packaging/out/`.
- `packaging/rehearse-ansible.sh <podman-network>` runs the Ansible playbook in `deploy/ansible/` for real in an EL9 container with systemd.
- `deploy/apache-pwa.conf` is the Apache configuration; `deploy/ansible/configs/pwa/README.md` describes an AGLT2-style deployment.

EL9's Jinja2 is 2.11, older than the 3.0 that `requirements-dev.txt` installs; the rehearsal is what checks the real combination.

## Commands

```
python -m pwa init-db                   create the tables
python -m pwa import-pwa <export-dir>   import a PWA database export, then publish
python -m pwa lookup-sync [--dry-run]   refresh hosts from the Lookup Service, then publish
python -m pwa publish                   publish everything again
python -m pwa serve [--port N]          development server
python -m pwa mcp --as <login>         MCP server for AI assistants (docs/MCP.md)
```

Settings are environment variables, or `KEY=value` lines in the file named by `PWA_CONFIG`; `pwa/config.py` lists them.

## Licence

Apache License 2.0; see [LICENSE](LICENSE). The bundled files below keep their own licences.

## Security

Please report security problems privately: see [SECURITY.md](SECURITY.md).

## Logo

The logo kit (icon, favicons, lockups for light and dark backgrounds, social card, colour values) is in [docs/brand](docs/brand/README.txt). The application uses copies in `pwa/web/static/brand/`. The wordmark is set in Inter (SIL Open Font License), converted to outlines.

## Bundled third-party files

- `pwa/specs/tests/*.json`, `pwa/specs/archivers/*.json`: `spec-jsonschema.json` files from perfsonar/pscheduler, branch 5.3.0 (commit a66acdc6), Apache License 2.0.
- `pwa/specs/psconfig-schema.json`: from perfsonar/psconfig v5.2.6, Apache License 2.0.
- `pwa/web/static/htmx.min.js`: htmx 1.9.12, BSD 2-Clause (sha256 449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452).
