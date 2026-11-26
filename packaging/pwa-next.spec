Name:           pwa-next
Version:        0.2.1
Release:        1%{?dist}
Summary:        Web administration for perfSONAR pSConfig meshes

License:        Apache-2.0
URL:            https://github.com/osg-htc/pwa-next
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  systemd-rpm-macros

Requires:       python3-flask >= 2.0
Requires:       python3-sqlalchemy >= 1.4
Requires:       python3-jsonschema >= 3.2
Requires:       python3-psycopg2
Requires:       httpd
Requires:       mod_ssl
Requires:       python3-mod_wsgi
Requires(pre):  shadow-utils
Recommends:     mod_auth_openidc
Recommends:     postgresql-server
%{?systemd_requires}

%description
Define perfSONAR tests, host groups and meshes in a browser and publish them
as pSConfig templates.  A replacement for pSConfig Web Admin (PWA) built on
Flask, PostgreSQL and Apache.

%prep
%autosetup

%build
%py3_build

%install
%py3_install
install -D -m 0644 deploy/pwa.wsgi                     %{buildroot}%{_datadir}/%{name}/pwa.wsgi
install -D -m 0640 packaging/pwa.env                   %{buildroot}%{_sysconfdir}/pwa/pwa.env
install -D -m 0644 packaging/pwa-lookup-sync.service   %{buildroot}%{_unitdir}/pwa-lookup-sync.service
install -D -m 0644 packaging/pwa-lookup-sync.timer     %{buildroot}%{_unitdir}/pwa-lookup-sync.timer
install -d -m 0755 %{buildroot}%{_sharedstatedir}/pwa/pub
ln -s %{python3_sitelib}/pwa/web/static %{buildroot}%{_datadir}/%{name}/static
install -D -m 0755 tools/check_import.py   %{buildroot}%{_datadir}/%{name}/tools/check_import.py
install -D -m 0755 tools/fetch_baseline.py %{buildroot}%{_datadir}/%{name}/tools/fetch_baseline.py
install -D -m 0755 tools/export_auth_users.py %{buildroot}%{_datadir}/%{name}/tools/export_auth_users.py

%pre
getent group pwa >/dev/null || groupadd -r pwa
getent passwd pwa >/dev/null || \
    useradd -r -g pwa -d %{_sharedstatedir}/pwa -s /sbin/nologin -c "pSConfig Web Admin" pwa
exit 0

%post
%systemd_post pwa-lookup-sync.timer

%preun
%systemd_preun pwa-lookup-sync.timer

%postun
%systemd_postun pwa-lookup-sync.timer

%files
%license LICENSE
%doc README.md DESIGN.md docs/MIGRATION.md deploy/apache-pwa.conf
%{python3_sitelib}/pwa/
%{python3_sitelib}/pwa_next-%{version}-py%{python3_version}.egg-info/
%{_bindir}/pwa
%{_datadir}/%{name}/
%{_unitdir}/pwa-lookup-sync.service
%{_unitdir}/pwa-lookup-sync.timer
%dir %attr(0750,root,pwa) %{_sysconfdir}/pwa
%config(noreplace) %attr(0640,root,pwa) %{_sysconfdir}/pwa/pwa.env
%dir %attr(0755,pwa,pwa) %{_sharedstatedir}/pwa
%dir %attr(0755,pwa,pwa) %{_sharedstatedir}/pwa/pub

%changelog
* Thu Nov 26 2026 Shawn McKee - 0.2.1-1
- People: administrators can delete an account (its access, logins and team
  memberships; history kept), after a list of anything nobody else could change.
- Ansible: load mod_auth_openidc only for CILogon (RHEL 9's 2.4.16.11-1 crashes
  httpd workers when it is loaded unconfigured); fail2ban jail for failed web
  sign-ins.

* Thu Nov 26 2026 Shawn McKee - 0.2.0-1
- Help page, test-type help with tool/option support, Tests filters, Reports.
- Branding (settings page and `pwa branding`), pwa-next logo and favicons.
- Single-host test types published as pSConfig list groups.
- Security: no-store pages, framing and sniffing headers, debugger off,
  2 MB request limit; clearer explanations of test problems.
- Fixes: UTF-8 data files under Apache, filter drop-downs, PWA "streams" import.

* Fri Nov 20 2026 Shawn McKee - 0.1.0-1
- First package of the prototype.
