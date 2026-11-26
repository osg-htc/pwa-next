"""The next-generation Lookup Service format (perfsonar-pslookup): one record per interface."""
from pwa import lookup, models as m, reports


def new_record(interface, addresses, tests=("trace", "throughput"), **host_over):
    host = {"name": ["ps1.example.edu"], "client_uuid": "uuid-1",
            "location": {"sitename": "Example U", "country": "US", "city": "Ann Arbor"},
            "group_communities": ["WLCG", "ATLAS"], "perfsonar_version": "5.3.0-1.el9",
            "perfsonar_bundle": "perfsonar-toolkit", "os_name": "AlmaLinux", "vm": False,
            "administrators": [{"emails": ["admin@example.edu"], "meta": {"name": "A Admin"}}],
            "pscheduler_service": {"urls": ["https://ps1.example.edu/pscheduler"], "tests": ["rtt"],
                                   "tools": ["ping"]}}
    host.update(host_over)
    return {"name": interface, "addresses": list(addresses), "pscheduler_tests": list(tests), "host": host}


def test_interfaces_are_put_back_together_as_one_host():
    records = [new_record("eth0", ["192.0.2.5"]), new_record("eth1", ["2001:db8::5"], tests=("latencybg",)),
               {"type": ["host"], "host-name": ["old.example.org"]}]                 # current format: ignored
    services, hosts = lookup.from_new_format(records)
    assert len(services) == 1 and len(hosts) == 1
    h, s = hosts[0], services[0]
    assert h["host-name"] == ["ps1.example.edu", "192.0.2.5", "2001:db8::5"]
    assert h["location-sitename"] == ["Example U"] and h["location-country"] == ["US"]
    assert h["pshost-toolkitversion"] == ["5.3.0-1.el9"] and h["host-os-name"] == ["AlmaLinux"]
    assert "host-administrators" not in h                       # personal data is not carried over
    assert s["pscheduler-tests"] == ["latencybg", "rtt", "throughput", "trace"]
    assert s["service-host"] == [h["uri"]] and s["service-locator"] == ["https://ps1.example.edu/pscheduler"]
    assert lookup.is_new_format(records[0]) and not lookup.is_new_format(records[2])


def test_records_without_pscheduler_and_converted_records():
    no_ps = new_record("eth0", ["192.0.2.9"], tests=(), client_uuid="uuid-9", pscheduler_service=None)
    # what the server's own converter writes for hosts still on the old client
    converted = new_record("eth0", ["192.0.2.7"], client_uuid="uuid-7", name=["ps7.example.edu"],
                           perfsonar_version=None, meta={"perfsonar_version": "5.2.6"})
    services, hosts = lookup.from_new_format([no_ps, converted])
    assert [h["host-name"][0] for h in hosts] == ["ps7.example.edu"]
    assert hosts[0]["pshost-toolkitversion"] == ["5.2.6"]


def test_sync_treats_both_formats_as_one_inventory(app, db):
    new_services, new_hosts = lookup.from_new_format([new_record("eth0", ["192.0.2.5"])])
    old_service = {"type": ["service"], "service-type": ["pscheduler"], "service-host": ["lookup/host/1"],
                   "service-locator": ["https://ps1.example.edu/pscheduler"], "pscheduler-tests": ["latency"]}
    old_host = {"type": ["host"], "uri": "lookup/host/1", "host-name": ["ps1.example.edu"],
                "location-sitename": ["Example U"], "pshost-toolkitversion": ["5.2.6"]}
    summary = lookup.sync(db, new_services + [old_service], new_hosts + [old_host], resolve=None)
    assert summary["created"] == ["ps1.example.edu"]
    h = db.query(m.Host).filter_by(hostname="ps1.example.edu").one()
    assert h.site == "Example U" and h.communities == ["ATLAS", "WLCG"]
    assert set(h.capabilities) == {"latency", "rtt", "throughput", "trace"}     # both registrations count


def test_a_5_3_nightly_in_the_current_format_is_understood():
    """5.3.0 nightlies still register through lsregistrationdaemon, with versions like this."""
    service = {"type": ["service"], "service-type": ["pscheduler"], "service-host": ["lookup/host/9"],
               "service-locator": ["https://perfsonar-deb.example.no/pscheduler"], "pscheduler-tests": ["trace"]}
    host = {"type": ["host"], "uri": "lookup/host/9", "host-name": ["perfsonar-deb.example.no"],
            "pshost-bundle-version": ["5.3.0~a1.0~20261125031803"],
            "pshost-toolkitversion": ["5.3.0~a1.0~20261125031803"]}
    fields = lookup.describe(service, host)
    assert fields["info"]["pshost-toolkitversion"].startswith("5.3.0")
    h = m.Host(hostname="perfsonar-deb.example.no", source="lookup", info=fields["info"])
    assert reports.action_for(h) == "check"            # current software: only check that it registers
