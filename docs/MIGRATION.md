# Moving from pSConfig Web Admin

The import is one-way and reads a copy of PWA's data; it never writes to PWA.

## 1. Export PWA's database

On the PWA host (Docker deployment; the database is `mca`):

```
for c in hosts hostgroups testspecs archives configs; do
  docker exec <mongo-container> mongoexport --quiet --db mca --collection $c --jsonArray > export/$c.json
done
```

The export contains host details and admin ids; keep it readable by root only.

Then add the people behind PWA's numeric admin ids, from the auth service's SQLite database (in the Docker deployment, the volume mounted at `/db` in the `sca-auth` container):

```
python3 tools/export_auth_users.py /path/to/auth.sqlite export
```

It reads names, e-mail addresses, federated identities and the administrator flag, never password hashes, and writes `export/users.json` readable by its owner only.

How each person gets their access back:

- **PWA knew their CILogon identity:** it works at their first login, with nothing to do.
- **They used a local password or a certificate** (most people, in the OSG/WLCG instance): the account imports as a placeholder. When someone signs in with the same e-mail address, administrators see a proposal on the overview page and confirm it with one click. It is not automatic, because the address is asserted by the person's identity provider.
- **No `users.json`:** the same, but placeholders have no names or addresses, so an administrator links each by hand (People, open the imported account, "Move this account's access").

## 2. Capture what PWA publishes

```
python3 tools/fetch_baseline.py export export/baseline --base https://HOST/pub --auto 40
```

Fetches every mesh and a sample of per-host feeds, one request at a time.

## 3. Check the import before trusting it

```
python3 tools/check_import.py export export/baseline
```

Imports into a scratch database, publishes, and compares task by task with the baseline. It prints the importer's warnings (for example hosts still archiving to esmond) and must end with `different: 0`.

## 4. Import for real

```
python3 -m pwa import-pwa export
```

Refuses to run against a database that already has hosts or meshes.

## How behaviour compares with PWA

| PWA | Here |
|---|---|
| A test spec pScheduler rejects is published anyway | The task is withheld and flagged until the test is fixed |
| Tasks carry no `reference` fields | Each task carries `display-task-name` and `display-task-group` for dashboards |
| Same-named tasks from two meshes merge in a host's feed | Both are kept, with the mesh address appended to the name |
| A full-mesh task with two groups silently tests all of them together | Imported as a group that includes both, so the behaviour is visible |
| A known host with no tests gets an empty template; unknown hosts get 404 | Same |
| Legacy MeshConfig format (`?format=meshconfig`) | Not available |
| Dynamic groups written in JavaScript | Members are frozen at import; re-create the selection as a rule |
