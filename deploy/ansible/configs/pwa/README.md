# Deploying pwa-next with the AGLT2 Ansible repository

Drafts for review. Nothing here has been committed to the AGLT2 SVN.

## What goes where

| File here | In the Ansible repository |
|---|---|
| `../../pwa.yaml` | `playbooks/pwa.yaml` |
| `pwa.env.j2`, `pwa.conf.j2`, `pwa-shadow-check.j2` | `configs/pwa/` |
| `pwa-export-for-shadow.sh` | not in Ansible: copied by hand to the production PWA host |

The playbook was rehearsed in an EL9 container with systemd (`packaging/rehearse-ansible.sh`): install, PostgreSQL setup, settings, Apache with password sign-in, timer, and a second run that changes nothing.

## Other changes the new host needs in the Ansible repository

1. **Inventory** (`inventory_public/03-agl.yaml`): a group `PWANext` with the new host (short and full name, as `PWAServer` has), and the host added to `WebServers` so `hostcert.yaml` installs its certificate and points `ssl.conf` at it.
2. **Firewall** (`configs/nftables/aglt2-nft.j2`), next to the `PWAServer` block:

   ```
   {% if "PWANext" in group_names | string %}
   # pwa-next: 80 redirects to https and serves /pub, 443 is the UI and /pub
   add rule inet aglt2_filter input_chain tcp dport { 80, 443 } accept
   {% endif %}
   ```
3. **Host variables**: `pwa_admins` (your sign-in name), and later `pwa_signin: oidc` and `pwa_shadow_of: psconfig.aglt2.org`.
4. **Certificate**: `http-<nodename>.pem` and `http-<nodename>-key.pem` in `/atlas/data08/manage/hostcert/`, as for other web servers.
5. **RPM**: `pwa-next-<version>.noarch.rpm` added to the UMATLAS repository on `umfs02` (`svn add`, `svn ci`, `sync_and_mirror.sh`).

## Nightly comparison with production (`pwa_shadow_of`)

The shadow host needs to read production's data without being able to do anything else there:

1. On the new host: `ssh-keygen -t ed25519 -N '' -f /root/.ssh/pwa_shadow_export`
2. On the production PWA host: install `pwa-export-for-shadow.sh` as `/usr/local/sbin/pwa-export-for-shadow` (mode 700), and add the new host's public key to `/root/.ssh/authorized_keys` as one line:

   ```
   restrict,command="/usr/local/sbin/pwa-export-for-shadow" ssh-ed25519 AAAA... pwa-shadow
   ```

   With `restrict` and a forced command, that key can only produce the export.
3. Set `pwa_shadow_of: psconfig.aglt2.org` and run the playbook. CheckMK then shows job `pwa_shadow`.
