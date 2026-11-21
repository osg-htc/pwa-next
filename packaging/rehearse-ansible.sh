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
in() { podman exec $name "$@"; }
failed=0
check() { if [ "$2" = "$3" ]; then echo "ok    $1"; else echo "FAIL  $1 (expected $2, got $3)"; failed=1; fi; }

echo "=== prepare the container (ansible-core, the RPM, the playbook laid out as in the site repository)"
in dnf -y -q install epel-release >/dev/null
in dnf -y -q install ansible-core procps-ng >/dev/null
podman cp "$rpm" $name:/root/pwa-next.rpm
in dnf -y -q install /root/pwa-next.rpm >/dev/null
in mkdir -p /root/ansible/playbooks /root/ansible/configs/pwa
podman cp "$here/deploy/ansible/pwa.yaml" $name:/root/ansible/playbooks/pwa.yaml
for f in pwa.env.j2 pwa.conf.j2 pwa-shadow-check.j2; do
    podman cp "$here/deploy/ansible/configs/pwa/$f" $name:/root/ansible/configs/pwa/$f
done
in bash -c 'printf "[PWANext]\nlocalhost ansible_connection=local\n" > /root/ansible/inventory'
play() { in bash -c "cd /root/ansible/playbooks && ansible-playbook -i ../inventory pwa.yaml -e pwa_public_name=localhost -e pwa_admins=tester $* 2>&1"; }

echo "=== first run: must stop and ask for the sign-in file"
out=$(play || true)
check "stops with a clear message" "1" "$(echo "$out" | grep -c 'pwa-htpasswd is missing')"
check "httpd was not started half-configured" "inactive" "$(in systemctl is-active httpd || true)"

echo "=== second run, after creating the password file by hand"
in bash -c 'htpasswd -B -b -c /etc/httpd/pwa-htpasswd tester testpw 2>/dev/null; chown root:apache /etc/httpd/pwa-htpasswd; chmod 640 /etc/httpd/pwa-htpasswd'
out=$(play || true)
echo "$out" | grep -E '^localhost' | sed 's/^/      /'
check "playbook succeeded" "1" "$(echo "$out" | grep -E '^localhost' | grep -c 'failed=0')"

echo "=== third run: nothing left to change"
out=$(play || true)
echo "$out" | grep -E '^localhost' | sed 's/^/      /'
check "idempotent" "1" "$(echo "$out" | grep -E '^localhost' | grep -c 'changed=0 .*failed=0')"

echo "=== the result"
code() { in curl -k -s -o /dev/null -w '%{http_code}' --retry 5 --retry-connrefused --retry-delay 1 "$@"; }
check "PostgreSQL is running and enabled" "active enabled" "$(in systemctl is-active postgresql) $(in systemctl is-enabled postgresql)"
check "database is UTF-8 and owned by pwa" "UTF8|pwa" "$(in runuser -u postgres -- psql -At -c "select pg_encoding_to_char(encoding), pg_get_userbyid(datdba) from pg_database where datname='pwa'")"
check "httpd is running and enabled" "active enabled" "$(in systemctl is-active httpd) $(in systemctl is-enabled httpd)"
check "session key is private" "640 root pwa" "$(in stat -c '%a %U %G' /etc/pwa/secret.key)"
check "published index, no login" "200" "$(code https://localhost/pub/config)"
check "published index over plain HTTP" "200" "$(code http://localhost/pub/config)"
check "application refuses anonymous visitors" "401" "$(code https://localhost/)"
check "signed-in overview" "200" "$(code -u tester:testpw https://localhost/)"
check "stylesheet without login" "200" "$(code https://localhost/static/pwa.css)"
check "inventory timer is running and enabled" "active enabled" "$(in systemctl is-active pwa-lookup-sync.timer) $(in systemctl is-enabled pwa-lookup-sync.timer)"

echo "=== the Lookup Service refresh, run once as the service would"
in systemctl start pwa-lookup-sync.service || true
check "refresh service succeeded" "success" "$(in systemctl show -p Result --value pwa-lookup-sync.service)"
hosts=$(in runuser -u postgres -- psql -At -d pwa -c 'select count(*) from hosts')
echo "      hosts in the inventory: $hosts"
check "inventory was filled" "yes" "$([ "$hosts" -gt 500 ] && echo yes || echo no)"
check "a per-host feed was published" "200" "$(code "https://localhost/pub/auto/$(in runuser -u postgres -- psql -At -d pwa -c 'select hostname from hosts order by hostname limit 1')")"

echo "=== variants render and parse"
out=$(play -e pwa_shadow_of=psconfig.example.org || true)
check "shadow comparison job installed" "1" "$(in grep -c 'mk-job pwa_shadow' /etc/cron.d/pwa_shadow.cron)"
check "shadow script is valid shell" "0" "$(in bash -n /usr/local/sbin/pwa-shadow-check; echo $?)"
in bash -c 'printf "OIDCClientID test\nOIDCClientSecret test\n" > /etc/httpd/pwa-oidc-client.conf; chmod 600 /etc/httpd/pwa-oidc-client.conf'
in bash -c "cd /root/ansible/playbooks && ansible localhost -i ../inventory -m template -a 'src=../configs/pwa/pwa.conf.j2 dest=/etc/httpd/conf.d/pwa.conf' -e pwa_signin=oidc -e pwa_public_name=localhost >/dev/null"
check "CILogon variant accepted by httpd" "Syntax OK" "$(in httpd -t 2>&1 | tr '\n' ' ' | sed 's/ $//')"

echo
if [ $failed = 0 ]; then echo "ANSIBLE REHEARSAL PASSED"; else echo "ANSIBLE REHEARSAL FAILED"; fi
exit $failed
