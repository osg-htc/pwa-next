# Deploying pwa-next with the AGLT2 Ansible repository

The AGLT2 copies live in the Ansible SVN (`playbooks/pwa.yaml`, `configs/pwa/`); keep them in step with these.

## What goes where

| File here | In the Ansible repository |
|---|---|
| `../../pwa.yaml` | `playbooks/pwa.yaml` |
| `pwa.env.j2`, `pwa.conf.j2`, `pwa-shadow-check.j2` | `configs/pwa/` |
| `branding-wlcg.json` (and optionally `branding-wlcg-logo.svg` or `.png`) | `configs/pwa/`; chosen per host with `pwa_branding: wlcg` |
| `registry-ca.pem` (CERN Root Certification Authority 2, SHA-256 fingerprint 18:7F:4F:7B:…:8A:32:0C) | `configs/pwa/`; trusted by `pwa site-sync` only, because WLCG CRIC's chain ends at this root and RHEL does not ship it |
| `pwa-export-for-shadow.sh` | not in Ansible: copied by hand to the production PWA host |

The playbook was rehearsed in an EL9 container with systemd (`packaging/rehearse-ansible.sh`): install, PostgreSQL setup, settings, Apache with password sign-in, timer, and a second run that changes nothing.

## Other changes the new host needs in the Ansible repository

Only the inventory (`inventory_public/03-agl.yaml`); the firewall template needs no change.

1. A group `PWANext` holding the host under its short and full names, with its settings as host variables (`pwa_public_name`, `pwa_admins`, `pwa_branding`, later `pwa_signin: oidc` and `pwa_shadow_of`).
2. `PWANext` as a child of `WebServers`. That brings three things that already exist for web servers:
   - `package.yaml` installs and starts httpd and mod_ssl;
   - `hostcert.yaml` installs the certificate and points `ssl.conf` at it;
   - the firewall opens 80 and 443 to the trusted networks only (UM, MSU, home and CERN networks).
3. `PWANext` as a child of `NoAFSclient`, like `PWAServer`.

Before the host joins `WebServers`, its certificate must be in `/atlas/data08/manage/hostcert/` as `http-<full host name>.pem` and `http-<full host name>-key.pem`. Without it `ssl.conf` points at a file that does not exist and httpd cannot start.

When toolkits and people outside the trusted networks need to reach the host, add it to `WebTrustAll`.

The RPM goes into the UMATLAS repository on `umfs02` (`svn add`, `svn ci`, `sync_and_mirror.sh`).

## Nightly comparison with production (`pwa_shadow_of`)

The shadow host needs to read production's data without being able to do anything else there:

1. On the new host: `ssh-keygen -t ed25519 -N '' -f /root/.ssh/pwa_shadow_export`
2. On the production PWA host: install `pwa-export-for-shadow.sh` as `/usr/local/sbin/pwa-export-for-shadow` (mode 700), and add the new host's public key to `/root/.ssh/authorized_keys` as one line:

   ```
   restrict,command="/usr/local/sbin/pwa-export-for-shadow" ssh-ed25519 AAAA... pwa-shadow
   ```

   With `restrict` and a forced command, that key can only produce the export.
3. Set `pwa_shadow_of: psconfig.aglt2.org` and run the playbook. CheckMK then shows job `pwa_shadow`.
