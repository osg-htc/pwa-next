#!/bin/bash
# Run deploy/ansible/pwa.yaml for real inside an EL9 container with systemd.
#
#   packaging/rehearse-ansible.sh <podman-network>
#
# Needs the RPM in packaging/out/ (packaging/rehearse.sh builds it) and a
# podman network the container can reach package mirrors through.  Nothing is
# published to the host: all checks run inside the container.
set -euo pipefail
net=${1:?usage: rehearse-ansible.sh <podman-network>}
here=$(cd "$(dirname "$0")/.." && pwd)
rpm=$(ls "$here"/packaging/out/pwa-next-*.noarch.rpm | tail -1)
name=pwa-ansible-rehearsal
podman rm -f $name >/dev/null 2>&1 || true
podman run -d --name $name --systemd=always --network "$net" \
    --dns "$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf)" docker.io/almalinux/9-init >/dev/null
trap 'podman rm -f $name >/dev/null' EXIT
inside() { podman exec $name "$@"; }
failed=0
check() { if [ "$2" = "$3" ]; then echo "ok    $1"; else echo "FAIL  $1 (expected $2, got $3)"; failed=1; fi; }

echo "=== prepare the container (ansible-core, the RPM, the playbook laid out as in the site repository)"
inside dnf -y -q install epel-release >/dev/null
inside dnf -y -q install ansible-core procps-ng cronie httpd-tools >/dev/null
podman cp "$rpm" $name:/root/pwa-next.rpm
inside dnf -y -q install /root/pwa-next.rpm >/dev/null
inside mkdir -p /root/ansible/playbooks /root/ansible/configs/pwa
podman cp "$here/deploy/ansible/pwa.yaml" $name:/root/ansible/playbooks/pwa.yaml
for f in "$here"/deploy/ansible/configs/pwa/*; do
    podman cp "$f" $name:/root/ansible/configs/pwa/
done
# Settings arrive as inventory host variables, as they will at the site.
inside bash -c 'printf "[PWANext]\nlocalhost ansible_connection=local pwa_public_name=localhost pwa_admins=tester pwa_branding=wlcg\n" > /root/ansible/inventory'
play() { inside bash -c "cd /root/ansible/playbooks && ansible-playbook -i ../inventory pwa.yaml $* 2>&1"; }

echo "=== first run: must stop and ask for the sign-in file"
out=$(play || true)
check "stops with a clear message" "1" "$(echo "$out" | grep -c 'pwa-htpasswd is missing')"
echo "$out" | grep -E 'msg|^localhost' | cut -c1-220 | sed 's/^/      /' 
check "httpd was not started half-configured" "inactive" "$(inside systemctl is-active httpd || true)"

echo "=== second run, after creating the password file by hand"
inside bash -c 'htpasswd -B -b -c /etc/httpd/pwa-htpasswd tester testpw 2>/dev/null; chown root:apache /etc/httpd/pwa-htpasswd; chmod 640 /etc/httpd/pwa-htpasswd'
out=$(play || true)
echo "$out" | grep -E '^localhost' | sed 's/^/      /'
check "playbook succeeded" "1" "$(echo "$out" | grep -E '^localhost' | grep -c 'failed=0')"

echo "=== third run: nothing left to change"
out=$(play || true)
echo "$out" | grep -E '^localhost' | sed 's/^/      /'
check "idempotent" "1" "$(echo "$out" | grep -E '^localhost' | grep -c 'changed=0 .*failed=0')"

echo "=== the result"
code() { inside curl -k -s -o /dev/null -w '%{http_code}' --retry 5 --retry-connrefused --retry-delay 1 "$@"; }
check "PostgreSQL is running and enabled" "active enabled" "$(inside systemctl is-active postgresql) $(inside systemctl is-enabled postgresql)"
check "database is UTF-8 and owned by pwa" "UTF8|pwa" "$(inside runuser -u postgres -- psql -At -c "select pg_encoding_to_char(encoding), pg_get_userbyid(datdba) from pg_database where datname='pwa'")"
check "httpd is running and enabled" "active enabled" "$(inside systemctl is-active httpd) $(inside systemctl is-enabled httpd)"
check "session key is private" "640 root pwa" "$(inside stat -c '%a %U %G' /etc/pwa/secret.key)"
check "published index, no login" "200" "$(code https://localhost/pub/config)"
check "published index over plain HTTP" "200" "$(code http://localhost/pub/config)"
check "application refuses anonymous visitors" "401" "$(code https://localhost/)"
check "signed-in overview" "200" "$(code -u tester:testpw https://localhost/)"
if ls "$here"/deploy/ansible/configs/pwa/branding-wlcg-logo.* >/dev/null 2>&1; then
    check "the branding's logo is served" "200" "$(code -u tester:testpw https://localhost/branding/logo)"
fi
check "comes up with the WLCG branding" "1" "$(inside curl -k -s -u tester:testpw https://localhost/ | grep -c '<title>Overview · WLCG perfSONAR Configuration (test)</title>')"
check "inventory variable made the first user an administrator" "t" "$(inside runuser -u postgres -- psql -At -d pwa -c "select is_admin from users where subject='tester'")"
check "stylesheet without login" "200" "$(code https://localhost/static/pwa.css)"
check "inventory timer is running and enabled" "active enabled" "$(inside systemctl is-active pwa-lookup-sync.timer) $(inside systemctl is-enabled pwa-lookup-sync.timer)"

echo "=== the Lookup Service refresh, run once as the service would"
inside systemctl start pwa-lookup-sync.service || true
check "refresh service succeeded" "success" "$(inside systemctl show -p Result --value pwa-lookup-sync.service)"
hosts=$(inside runuser -u postgres -- psql -At -d pwa -c 'select count(*) from hosts')
echo "      hosts in the inventory: $hosts"
check "inventory was filled" "yes" "$([ "$hosts" -gt 500 ] && echo yes || echo no)"
check "a per-host feed was published" "200" "$(code "https://localhost/pub/auto/$(inside runuser -u postgres -- psql -At -d pwa -c 'select hostname from hosts order by hostname limit 1')")"

echo "=== the site registries (WLCG CRIC, GOCDB, OSG Topology), run once as the service would"
check "site registry timer is running and enabled" "active enabled" "$(inside systemctl is-active pwa-site-sync.timer) $(inside systemctl is-enabled pwa-site-sync.timer)"
check "registry CA installed" "1" "$(inside openssl x509 -in /etc/pwa/registry-ca.pem -noout -subject 2>/dev/null | grep -c 'CERN Root Certification Authority 2')"
check "registry CA set for pwa" "1" "$(inside grep -c '^PWA_REGISTRY_CA=/etc/pwa/registry-ca.pem' /etc/pwa/pwa.env)"
inside systemctl start pwa-site-sync.service || true
check "site registry service succeeded" "success" "$(inside systemctl show -p Result --value pwa-site-sync.service)"
matched=$(inside runuser -u postgres -- psql -At -d pwa -c "select count(*) from settings, json_object_keys((value->'hosts')::json) where key='site_registry'")
echo "      inventory hosts matched to a site: $matched"
check "registries matched hosts" "yes" "$([ "${matched:-0}" -gt 50 ] && echo yes || echo no)"

echo "=== variants render and parse"
out=$(play -e pwa_shadow_of=psconfig.example.org || true)
check "shadow comparison job installed" "1" "$(inside grep -c 'mk-job pwa_shadow' /etc/cron.d/pwa_shadow.cron)"
check "shadow script is valid shell" "0" "$(inside bash -n /usr/local/sbin/pwa-shadow-check; echo $?)"
inside bash -c 'printf "OIDCClientID test\nOIDCClientSecret test\n" > /etc/httpd/pwa-oidc-client.conf; chmod 600 /etc/httpd/pwa-oidc-client.conf'
out=$(play -e pwa_signin=oidc)             # the real path: the playbook also loads mod_auth_openidc
check "CILogon run of the playbook" "1" "$(echo "$out" | grep -E '^localhost' | grep -c 'failed=0')"
check "CILogon variant accepted by httpd" "1" "$(inside httpd -t 2>&1 | grep -c 'Syntax OK')"
check "CILogon module loaded" "1" "$(inside httpd -M 2>/dev/null | grep -c auth_openidc)"
out=$(play)                                 # and back to password sign-in: the module is unloaded again
check "password sign-in again, module unloaded" "0" "$(inside httpd -M 2>/dev/null | grep -c auth_openidc)"
check "httpd still accepts it" "1" "$(inside httpd -t 2>&1 | grep -c 'Syntax OK')"

echo
if [ $failed = 0 ]; then echo "ANSIBLE REHEARSAL PASSED"; else echo "ANSIBLE REHEARSAL FAILED"; fi
exit $failed
