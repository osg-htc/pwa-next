# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull request.

- E-mail **smckee@umich.edu** with "pwa-next security" in the subject.
- Or use GitHub's private vulnerability reporting: the "Report a vulnerability" button on the [Security tab](https://github.com/osg-htc/pwa-next/security).

Please include what you found, how to reproduce it, the version or commit, and what an attacker could do with it. If you have a fix in mind, describe it, but please do not open a public pull request for it until the problem has been fixed and announced.

What to expect:

- an acknowledgement within 5 working days;
- an assessment, and a plan if a fix is needed, within 14 days;
- a fix released as soon as it is ready, with credit to you in the release notes unless you would rather not be named.

## Supported versions

pwa-next is a prototype. Security fixes go into the `main` branch and the newest release only.

| Version | Supported |
|---|---|
| `main`, newest release | yes |
| older releases | no: upgrade |

## Scope

In scope:

- the application in `pwa/` (web pages, sign-in handling, permissions, the publisher and the files it writes);
- the packaging and deployment files in `packaging/` and `deploy/`, including the Apache configuration.

Out of scope (please report these to their own projects):

- perfSONAR, pScheduler, pSConfig and the Lookup Service;
- Flask, Werkzeug, Jinja2, SQLAlchemy and the other libraries, and Apache, `mod_wsgi` and `mod_auth_openidc`;
- the configuration of a particular deployment, which belongs to whoever runs it.

## How pwa-next is meant to be deployed

Some behaviour that may look like a weakness is by design. Reports that depend on a deployment going against these points are still welcome, as documentation fixes.

- **Apache signs people in** (CILogon through `mod_auth_openidc`, or a password file for testing). The application trusts the identity Apache passes it and must never be reachable without Apache in front.
- **Everyone who signs in can read everything.** Permissions control who can change things.
- **Published files are public**: `/pub/config/...` and `/pub/auto/...` are read by perfSONAR hosts anywhere without signing in. Nothing secret belongs in a test or an archive definition, because both are published.
- **`PWA_AUTH=dev`** lets anyone sign in as anyone. It is for a developer's own machine only.
- **Library versions:** EL9 deployments use the distribution's packages, including Flask and Werkzeug 2.0.3 from EPEL, which receive no security backports. [DESIGN.md](DESIGN.md#dependencies-and-security) lists the known issues in those versions, whether each applies, and how it is covered.
