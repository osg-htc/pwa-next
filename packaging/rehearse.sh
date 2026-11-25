#!/bin/bash
# Build the RPM and rehearse a deployment inside a clean EL9 container.
#
# Run from the repository root on a machine with podman:
#
#   packaging/rehearse.sh [PWA-EXPORT-DIR]
#
# It builds the RPM from the committed tree (git archive HEAD), installs it
# with EL9/EPEL's own packages, runs the test suite against SQLite and
# PostgreSQL, then starts Apache + mod_wsgi with password sign-in and checks
# the pages and the published files.  With an export directory (see
# docs/MIGRATION.md) it imports that data first.  The RPM is left in
# packaging/out/.  Apache listens on 127.0.0.1:18080 and :18443 only.
set -euo pipefail

if [ "${1:-}" != "--inside" ]; then
    here=$(cd "$(dirname "$0")/.." && pwd)
    version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$here/pwa/__init__.py")
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    git -C "$here" archive --prefix="pwa-next-$version/" -o "$work/pwa-next-$version.tar.gz" HEAD
    cp "$here/packaging/rehearse.sh" "$work/"
    mkdir -p "$here/packaging/out" "$work/export"
    if [ -n "${1:-}" ]; then cp -r "$1"/. "$work/export/"; fi
    # Host networking: the build needs DNS, and nothing here listens beyond loopback.
    podman run --rm --network host -v "$work:/work:Z" -v "$here/packaging/out:/out:Z" \
        docker.io/library/almalinux:9 bash /work/rehearse.sh --inside "$version"
    exit $?
fi

version=$2
step() { printf '\n=== %s\n' "$*"; }
check() {   # check <description> <expected> <actual>
    if [ "$2" = "$3" ]; then echo "ok    $1"; else echo "FAIL  $1 (expected $2, got $3)"; failed=1; fi
}
failed=0

step "install build and runtime packages"
dnf -y -q install epel-release >/dev/null
dnf -y -q install rpm-build python3-devel python3-setuptools systemd-rpm-macros python3-pytest \
    python3-flask python3-sqlalchemy python3-jsonschema python3-psycopg2 httpd mod_ssl \
    python3-mod_wsgi mod_auth_openidc postgresql-server httpd-tools procps-ng >/dev/null
rpm -q --qf '%-22{NAME} %{VERSION}-%{RELEASE}  (%{VENDOR})\n' python3-flask python3-werkzeug \
    python3-jinja2 python3-sqlalchemy python3-jsonschema python3-psycopg2 python3-mod_wsgi \
    mod_auth_openidc postgresql-server httpd
for p in python3-flask python3-werkzeug python3-jinja2; do
    echo "$p: $(rpm -q --changelog $p | grep -i -c 'CVE-' || true) CVE fix(es) in the package changelog"
done

step "build the RPM"
mkdir -p ~/rpmbuild/SOURCES
cp /work/pwa-next-$version.tar.gz ~/rpmbuild/SOURCES/
tar -xzf /work/pwa-next-$version.tar.gz -C /tmp
rpmbuild -ba /tmp/pwa-next-$version/packaging/pwa-next.spec >/tmp/rpmbuild.log 2>&1 || { tail -30 /tmp/rpmbuild.log; exit 1; }
rpm=$(ls ~/rpmbuild/RPMS/noarch/pwa-next-$version-*.noarch.rpm)
cp "$rpm" ~/rpmbuild/SRPMS/*.src.rpm /out/
echo "built $(basename "$rpm"): $(rpm -qlp "$rpm" | wc -l) files"

step "install it"
dnf -y -q install "$rpm" >/dev/null
check "service account exists" "pwa" "$(id -un pwa)"
check "settings file is private" "640 root pwa" "$(stat -c '%a %U %G' /etc/pwa/pwa.env)"
check "command works" "0" "$(pwa --help >/dev/null 2>&1; echo $?)"

step "test suite against the installed package, SQLite"
mkdir -p /tmp/t && cp -r /tmp/pwa-next-$version/tests /tmp/t/ && chmod -R a+rX /tmp/t
cd /tmp/t
python3 -c 'import pwa; print("using", pwa.__file__)'
python3 -m pytest -q -p no:cacheprovider tests 2>&1 | tail -3
check "tests pass on SQLite" "0" "${PIPESTATUS[0]}"

step "PostgreSQL $(rpm -q --qf '%{VERSION}' postgresql-server)"
mkdir -p /run/postgresql && chown postgres:postgres /run/postgresql
su postgres -c "initdb -D /var/lib/pgsql/data -A peer -E UTF8 --locale=C.UTF-8 >/dev/null"
su postgres -c "pg_ctl -D /var/lib/pgsql/data -o \"-c listen_addresses=''\" -w start >/dev/null"
su postgres -c "createuser pwa && createdb -O pwa pwa && createdb -O pwa pwa_test"
su pwa -s /bin/bash -c "cd /tmp/t && PWA_TEST_DATABASE_URL=postgresql:///pwa_test python3 -m pytest -q -p no:cacheprovider --tb=line tests" 2>&1 | tail -6
check "tests pass on PostgreSQL (socket, peer authentication)" "0" "${PIPESTATUS[0]}"

step "configure"
key=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
sed -i -e "s|^PWA_PUBLIC_URL=.*|PWA_PUBLIC_URL=https://localhost:18443/pub|" \
       -e "s|^PWA_SECRET_KEY=.*|PWA_SECRET_KEY=$key|" -e "s|^PWA_ADMINS=.*|PWA_ADMINS=tester|" /etc/pwa/pwa.env
as_pwa() { su pwa -s /bin/bash -c "PWA_CONFIG=/etc/pwa/pwa.env $*"; }
as_pwa "pwa init-db"
if [ -f /work/export/configs.json ]; then
    cp -r /work/export /tmp/export && chown -R pwa /tmp/export
    as_pwa "pwa import-pwa /tmp/export" | grep -v -E '^warning: (host|test) ' | cut -c1-200
else
    as_pwa "pwa publish"
fi

step "Apache + mod_wsgi, password sign-in"
cp /usr/share/doc/pwa-next/apache-pwa.conf /etc/httpd/conf.d/pwa.conf
sed -i '/option B/,/end of sign-in/ s/^#\(<Location\|    Auth\|    Require\|<\/Location\)/\1/' /etc/httpd/conf.d/pwa.conf
htpasswd -B -b -c /etc/httpd/pwa-htpasswd tester testpw 2>/dev/null
chown root:apache /etc/httpd/pwa-htpasswd && chmod 640 /etc/httpd/pwa-htpasswd
sed -i 's/^Listen 80$/Listen 127.0.0.1:18080/' /etc/httpd/conf/httpd.conf
sed -i -e 's/^Listen 443 https/Listen 127.0.0.1:18443 https/' -e 's/_default_:443/127.0.0.1:18443/' /etc/httpd/conf.d/ssl.conf
echo "ServerName localhost" > /etc/httpd/conf.d/servername.conf
/usr/libexec/httpd-ssl-gencerts
mkdir -p /run/httpd
check "configuration is valid, no warnings" "Syntax OK" "$(httpd -t 2>&1 | tr '\n' ' ' | sed 's/ $//')"
httpd -k start
code() { curl -k -s -o /dev/null -w '%{http_code}' --retry 5 --retry-connrefused --retry-delay 1 "$@"; }
S=https://localhost:18443
check "published index needs no login" "200" "$(code $S/pub/config)"
check "published index is JSON" "application/json" "$(curl -k -s -o /dev/null -w '%{content_type}' $S/pub/config)"
check "published files also over plain HTTP" "200" "$(code http://127.0.0.1:18080/pub/config)"
check "application redirects plain HTTP to HTTPS" "301" "$(code http://127.0.0.1:18080/)"
check "application refuses anonymous visitors" "401" "$(code $S/)"
check "wrong password refused" "401" "$(code -u tester:wrong $S/)"
check "signed-in overview" "200" "$(code -u tester:testpw $S/)"
for page in meshes groups tests hosts archives access audit; do
    check "page /$page" "200" "$(code -u tester:testpw $S/$page)"
done
check "stylesheet served by Apache itself, no login" "200 text/css" "$(curl -k -s -o /dev/null -w '%{http_code} %{content_type}' $S/static/pwa.css)"
check "logo served by Apache, no login" "200 image/svg+xml" "$(curl -k -s -o /dev/null -w '%{http_code} %{content_type}' $S/static/brand/pwa-next-icon.svg)"
check "favicon, no login" "200" "$(code $S/favicon.ico)"
check "first sign-in created an administrator" "t" \
    "$(su postgres -c "psql -At -d pwa -c \"select is_admin from users where subject='tester'\"")"
# A change through the browser path: session cookie + CSRF token, repeated so
# that both mod_wsgi processes get to serve it.
jar=/tmp/jar
token=$(curl -k -s -u tester:testpw -c $jar $S/ | sed -n 's/.*data-csrf="\([0-9a-f]*\)".*/\1/p')
ok=0
for i in 1 2 3 4 5 6; do
    [ "$(code -u tester:testpw -b $jar --form-string "_csrf=$token" $S/publish)" = "302" ] && ok=$((ok + 1))
done
check "form posts accepted by every worker process" "6" "$ok"
check "oversized request body refused by Apache" "413" "$(head -c 3000000 /dev/zero | curl -k -s -o /dev/null -w '%{http_code}' -u tester:testpw -X POST --data-binary @- $S/publish)"
check "pages not cacheable, vary on cookie" "1" "$(curl -k -s -D - -o /dev/null -u tester:testpw $S/meshes | grep -c -i -E '^cache-control: private, no-store')"
check "form post without the token refused" "400" "$(code -u tester:testpw -b $jar -X POST $S/publish)"
if [ -f /work/export/configs.json ]; then
    slug=$(python3 -c 'import json; print(json.load(open("/tmp/export/configs.json"))[0]["url"])')
    check "an imported mesh is served" "200" "$(code $S/pub/config/$slug)"
    python3 /tmp/pwa-next-$version/tools/check_import.py /tmp/export /tmp/export/baseline 2>/dev/null | tail -1
fi
check "unknown host feed" "404" "$(code $S/pub/auto/no-such-host.example.org)"

step "CILogon variant of the configuration parses"
sed -e '/option A/,/option B/ s/^#\(OIDC\|<Location\|    Auth\|    Require\|<\/Location\)/\1/' \
    -e '/option B/,/end of sign-in/ s/^\(<Location\|    Auth\|    Require\|<\/Location\)/#\1/' \
    /etc/httpd/conf.d/pwa.conf > /tmp/pwa-oidc.conf
cp /etc/httpd/conf.d/pwa.conf /tmp/pwa-basic.conf && cp /tmp/pwa-oidc.conf /etc/httpd/conf.d/pwa.conf
check "mod_auth_openidc directives accepted" "Syntax OK" "$(httpd -t 2>&1 | tail -1)"
cp /tmp/pwa-basic.conf /etc/httpd/conf.d/pwa.conf

step "errors logged by Apache during the run"
grep -c -i -E 'traceback|\[wsgi:error\]' /var/log/httpd/*error_log | sed 's/^/  /' || true
if [ $failed != 0 ]; then
    echo "--- last error log lines"
    tail -n 40 /var/log/httpd/ssl_error_log /var/log/httpd/error_log | cut -c1-300
fi

httpd -k stop
su postgres -c "pg_ctl -D /var/lib/pgsql/data -m fast stop >/dev/null"
echo
if [ $failed = 0 ]; then echo "REHEARSAL PASSED"; else echo "REHEARSAL FAILED"; fi
exit $failed
