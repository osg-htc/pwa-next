# Design: a replacement for pSConfig Web Admin

Status: working prototype, 2026-11-20. Not deployed anywhere.
Working name `pwa-next`; the Python package is `pwa`.

## What it is for

pSConfig Web Admin (PWA) lets a community define perfSONAR meshes in a browser and publishes them as pSConfig templates that toolkits read. The OSG/WLCG instance publishes 32 meshes to roughly 270 hosts. PWA is deprecated upstream, and its Node 16 / AngularJS / MongoDB stack is end-of-life. Its intended successor, pSCompose, is a single-administrator editor without a host inventory, federated login or per-item permissions.

This is a rebuild of what PWA does, using only technology perfSONAR already ships, with the effort spent on the things mesh administrators do every day.

## Goals

1. **Same published result.** Toolkits keep their URLs (`/pub/config/<mesh>`, `/pub/auto/<hostname>`) and get the same tasks.
2. **A UI built around choosing, not typing.** Options come from drop-downs and type-ahead search; forms are generated from pScheduler's own schemas.
3. **Automation of chores.** Removing a host from everything, groups that follow other groups or a rule, "where is this used", copies, bulk adds.
4. **Federated, delegated management.** Sign-in through CILogon (home-institution login, including eduGAIN identity providers); teams and roles decide who may change what.
5. **Nothing invalid gets published.** Specs are validated when saved and again when published.
6. **Small and conventional**, so it can be maintained by people who know the rest of perfSONAR.

Not goals: the legacy MeshConfig output format (perfSONAR 5.1 removed the translator; nothing requests it), and MongoDB compatibility.

## Technology

Everything below is already in a perfSONAR 5.2.6 install or in EL9's own repositories.

| Part | Choice | Where perfSONAR already uses it |
|---|---|---|
| Language | Python 3.9+ | pScheduler, pSConfig agents |
| Web framework | Flask 2.0, Jinja2 | pScheduler's API server |
| Database | PostgreSQL through SQLAlchemy 1.4 (SQLite for tests and development) | pScheduler (PostgreSQL), pSCompose (SQLAlchemy) |
| Web server | Apache with `mod_wsgi` | toolkit, pScheduler |
| Sign-in | Apache `mod_auth_openidc` talking to CILogon | new, but an EL9 AppStream package; the application contains no login code |
| Validation | `jsonschema` with pSConfig's and pScheduler's schema files | pSConfig, pScheduler |
| Browser | server-rendered pages, htmx 1.9 for partial updates, about 70 lines of plain JavaScript | pSCompose uses htmx |

There is no JavaScript build step and no Node dependency. The pinned development versions in `requirements-dev.txt` are the ones EL9 packages.

## How it fits together

```
                 CILogon (InCommon + eduGAIN identity providers)
                        |
  browser --> Apache: mod_auth_openidc --> mod_wsgi --> Flask app --> PostgreSQL
                                                          |
                                                    publish (on every change)
                                                          v
  toolkits --> Apache: static files <------------- PUBLISH_DIR/config/<mesh>
               (no application involved)                       /auto/<hostname>
                                                               /index.json
  timer: `pwa lookup-sync` --> Lookup Service (refresh host inventory, then publish)
```

### Publishing is static

Toolkits only read. Every template is rendered when something changes and written to a directory that Apache serves directly (`pwa/publish.py`). Consequences:

- The publisher cannot mix meshes under load (PWA issue #214), because nothing is generated per request.
- Published configurations stay available if the application or database is down.
- A full publish of the production data set (32 meshes, 1,666 per-host files) takes under a second, and files whose content did not change are not rewritten.
- A file replaces the live one only if it validates against the pSConfig schema. A task whose test pScheduler would reject is withheld on its own and flagged in the UI; the rest of the mesh still publishes.

### Data is stored in native form

Tests are stored as pScheduler specs, archives as pSConfig archive objects, intervals as ISO 8601 durations (`pwa/models.py`). Publishing assembles; it does not translate. PWA stored MeshConfig-era fields (`bwctl`, `ipv4_only`, seconds) and translated on every request, which is where issues #217 and #234 came from.

### Forms come from pScheduler's schemas

pScheduler 5.3 ships a JSON Schema per test and archiver plugin (`spec-jsonschema.json`). Copies are in `pwa/specs/`. `pwa/specforms.py` turns them into form fields (drop-down for enumerations, number limits, duration inputs, help text), validates what is entered, and works out the lowest spec `schema` number the options in use need. All 26 test types and 11 archivers are available without per-type code; supporting a new plugin means adding its schema file.

### Host groups

Effective members = hosts added by name + members of included groups + hosts matching a rule − excluded hosts (`pwa/services.py`).

- **Included groups** answer PWA issue #77: a traceroute group can include the bandwidth and latency groups and stays in step with them.
- **Rules** replace PWA's dynamic groups. A rule is a list of conditions on host name, site, Lookup Service community, offered test types or source, combined with all/any. It is data, not user-written JavaScript (PWA ran filters through the `sandbox` module).

### Host inventory

`pwa lookup-sync` (`pwa/lookup.py`) reads every live pScheduler service and host record from the Lookup Service and creates or refreshes hosts. A registration refreshes the inventory host that already goes by one of its names; the name a host advertises for its pScheduler is preferred, so a multi-homed host keeps one entry per interface. Hosts that stop registering are kept and flagged, never silently removed. This module is the only place that knows the Lookup Service record format, which is expected to change with perfSONAR 5.3.

### Sign-in and permissions

Apache authenticates; the application reads the subject and the name, e-mail and home-organisation claims from the WSGI environment and creates the account on first sight (`pwa/web/__init__.py`). Published files need no login.

Any signed-in person can read everything (the output is public anyway). Changing things needs a grant (`pwa/authz.py`): a **person or team** holds a **role** on **one item** or on **every item of a kind** (hosts, host groups, tests, archives, meshes).

| Role | Allows |
|---|---|
| owner | change, delete, decide who else has access |
| editor | change |
| contributor (host groups) | add and remove hosts only |
| creator (kind-wide) | create new items; the creator owns what they create |

A person can have several logins (CILogon issues a different identifier per identity provider); they all lead to one account. Service administrators can do everything, including managing people. Teams have managers who can add and remove members without an administrator. Every change is recorded with who made it and what changed (`audit_log`).

## What the UI does for the administrator

- **Type-ahead everywhere.** Adding a host to a group: type any part of a name or site, several words narrow the match, hosts that offer the group's test type come first, and hosts that do not advertise it are marked. Tests, host groups, people and teams are picked the same way.
- **Remove a host from everything** in one step, after a preview of the groups and meshes affected.
- **Where is this used** on every host, group, test and archive; nothing in use can be deleted by accident.
- **Sizes before you commit**: each task shows host counts and the number of test pairs it creates.
- **Copies** of tests, groups, archives and meshes (a copied mesh starts with its tasks disabled).
- **Needs attention** on the overview: withheld tasks, hosts that have gone quiet, archives using esmond, imported accounts not yet linked to a login.
- **Help on every test type**: what it does, why to use it, whether standard hosts have it, how many hosts in the inventory advertise it, and which tools accept which options. The tool/option table is measured by asking a live pScheduler (`tools/build_test_catalog.py`, stored in `pwa/specs/catalog.json`); the explanations are written by hand (`pwa/specs/guidance.json`). The test editor marks options only some tools support and refuses combinations pScheduler would reject.
- **Filters on the Tests page**: text, type, mesh, tool, schedule, IP version, and used, unused or with problems.
- **Reports**: per mesh (hosts, measurements, runs a day, throughput test hours), the busiest hosts by share of the day spent in exclusive throughput tests, host pairs measured more than once by the same kind of test, hosts that do not advertise a task's test type, perfSONAR versions in use, hosts gone quiet per mesh, and a lookup of everything tested between two hosts.
- **Single-host test types** (DNS, HTTP and others) are published as pSConfig `list` groups, so each host runs them on its own; test types that are not addressed by host (SNMP, disk-to-disk) cannot be put in a task.
- **Branding**: service administrators set the name, tagline, logo, colours, About text, contact and footer links on a Branding page; stored in the database, so no redeploy. Scripted deployments can set the same from a JSON file with `pwa branding set FILE [--logo IMAGE]` (example: `docs/examples/branding-wlcg.json`). Without an uploaded logo the pwa-next icon is shown. A Help page explains the concepts and common jobs. Inputs are validated (colours, link schemes, real image files), and the logo is served with a restrictive Content-Security-Policy so an SVG cannot run scripts.
- **Dashboard metadata**: every task carries `reference.display-task-name` and `display-task-group`, which the 5.x Grafana agent reads (PWA issue #229).

## Evidence so far

Checked on 2026-11-20 against the production OSG/WLCG PWA (read-only):

- Importing its database and publishing gives the same tasks, hosts, schedules, tools and archives as PWA serves, for all 32 meshes and 52 sampled per-host feeds (`tools/check_import.py`; the comparison resolves internal key names and was confirmed to detect deliberate changes).
- All 32 published mesh files pass `psconfig validate` from perfSONAR 5.2.6. Production PWA passes 31; the new publisher withholds the one task with an invalid test instead of publishing it.
- 61 automated tests cover forms, translation, publishing, membership, cascades, the web workflows and permissions.
- A dry run of the Lookup Service sync matched 1,299 existing inventory hosts by name.
- On PostgreSQL 15: the test suite passes, and the production import, a full publish (1.4 s) and a live Lookup Service sync all ran, with published output still matching PWA.

## Dependencies and security

Development and the test suite use current, patched releases (`requirements-dev.txt`). Production on EL9 uses the distribution's packages: Flask and Werkzeug 2.0.3 from EPEL, which carry no security backports, and Jinja2 2.11 and SQLAlchemy 1.4 from AppStream. `packaging/rehearse.sh` runs the whole test suite against exactly those, so the code is tested on both.

The known issues in the EL9 versions (GitHub's Dependabot listed 17 against the old pins on 2026-11-25), and why each does or does not matter here:

| Issue | Applies? | Cover |
|---|---|---|
| Flask: session cookie exposed through a shared cache when `Vary: Cookie` is missing (CVE-2023-30861, CVE-2026-27205) | Only behind a caching proxy, with permanent sessions; neither is used | Every page is sent with `Cache-Control: private, no-store` and `Vary: Cookie` |
| Werkzeug: CPU and memory exhaustion in multipart form parsing (CVE-2023-25577, CVE-2023-46136, CVE-2024-49767) | Partly: only signed-in people can post forms, because Apache authenticates first | Request bodies capped at 2 MB in Apache (`LimitRequestBody`) and in the application (`MAX_CONTENT_LENGTH`) |
| Werkzeug debugger remote code execution (CVE-2024-34069) | No: the debugger is never enabled | `app.debug` is forced off |
| Werkzeug `safe_join` and Windows device names (CVE-2024-49766, CVE-2025-66221, CVE-2026-21860, CVE-2026-27199, CVE-2026-102598) | No: Windows only | — |
| Werkzeug nameless cookies and `__Host-` cookies (CVE-2023-23934) | No: no `__Host-` cookies | — |
| Jinja2 sandbox escapes (CVE-2024-56201, CVE-2024-56326, CVE-2025-27516) | No: only the application's own templates are rendered, never in the sandbox | — |
| Jinja2 `xmlattr` attribute injection (CVE-2024-22195, CVE-2024-34064) | No: the filter is not used | — |

Pages also carry `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`. If a future issue does apply and EPEL does not fix it, the options are to package newer Flask and Werkzeug with pwa-next, or to move to EL10, whose EPEL has current Flask.

## Not done yet

| Item | Note |
|---|---|
| Deployment | Rehearsed in EL9 containers only (`packaging/rehearse.sh`, `packaging/rehearse-ansible.sh`): RPM build and install, PostgreSQL 13, Apache with mod_wsgi and password sign-in, and the Ansible playbook. Not yet on a real host. SELinux enforcing is not handled (AGLT2 runs permissive). |
| CILogon | The mod_auth_openidc configuration parses but has never met CILogon. Client registration, the claim names, and which eduGAIN identity providers CILogon admits all need confirming against a real login. |
| Linking the existing PWA accounts | Implemented (see docs/MIGRATION.md) but not yet exercised with a real CILogon login. In the OSG/WLCG instance only 6 of the 51 people who own something have a CILogon identity on record; the rest use local passwords and will need an administrator's one-click confirmation at first login. |
| Scripted access | `pwa mcp` (docs/MCP.md) gives AI assistants the read tools and five change tools, run locally on the host by its administrators. Not yet: remote MCP for everyone with a login (personal tokens or CILogon), a REST API. PWA's admin API is not reproduced. |
| pScheduler-side validation | Validation uses the bundled schemas; asking a live pScheduler (`psconfig validate` does) would catch what schemas cannot. |
| Schema migrations | Done (Alembic from RHEL 9 AppStream, `pwa/schema.py`, `pwa/migrations/`): every command brings the schema up to date; databases from 0.2.3 and older are stamped as the baseline first. A new table or column needs a model change AND a migration; `tests/test_schema.py` checks they agree. |
| Browser check | Pages were exercised over HTTP and by tests, but not looked at in a browser by the author. |
| Merging identical tasks | Implemented, off by default (`PWA_MERGE_DUPLICATES=trace`). In per-host feeds, pairs that several meshes measure with identical settings are carved out of each task (pSConfig `excludes`, one per direction) and measured once by a shared task listed under every mesh. On the 26 Nov WLCG data it removes 6,672 duplicate trace schedules (about 960,000 runs a day), loses no pair, keeps every feed valid, and grows the feeds from 6.7 to 9.1 MB. Turn it on only after checking that the dashboards show a shared task under each of its meshes; mesh files are never merged. |
| Lookup Service 5.3 format | Not implemented; the current format is. |

## Layout

```
pwa/models.py      tables                    pwa/services.py   membership, usage, cascades, audit
pwa/specforms.py   forms + validation        pwa/publish.py    templates and static publishing
pwa/authz.py       roles and grants          pwa/lookup.py     Lookup Service sync
pwa/importer.py    one-time import from PWA  pwa/compat.py     semantic template comparison
pwa/testinfo.py    test-type help, tool support  pwa/reports.py  configuration analyses
pwa/web/           Flask app, pages          pwa/specs/        schemas from pScheduler and pSConfig
pwa/mcp.py         MCP server (stdio)        pwa/cli.py        the `pwa` command
pwa/sites.py       site registries (CRIC, GOCDB, OSG Topology) for Reports > Follow-up with sites
tools/             baseline fetch, import check              tests/   pytest suite
```
