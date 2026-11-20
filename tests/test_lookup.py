import datetime

from pwa import lookup, models as m, services


def svc(host_uri, locator, tests=("throughput", "rtt", "trace")):
    return {"type": ["service"], "service-type": ["pscheduler"], "service-host": [host_uri],
            "service-locator": [locator], "pscheduler-tests": list(tests)}


def host(uri, names, site="Site", communities=("WLCG",)):
    return {"type": ["host"], "uri": uri, "host-name": names, "location-sitename": [site],
            "group-communities": list(communities), "pshost-toolkitversion": ["5.2.6"],
            "host-administrators": [""], "location-city": ["Ann Arbor"]}


PTR = {"192.0.2.10": "node10.example.org", "192.0.2.20": "old-name.example.org"}
resolve = lambda addresses: {a: PTR[a] for a in addresses if a in PTR}


def test_names_prefer_what_the_host_advertises():
    s, h = svc("lookup/host/1", "https://ps.example.org/pscheduler"), host("lookup/host/1", ["192.0.2.10"])
    assert lookup.candidate_names(s, h, PTR) == ["ps.example.org", "node10.example.org", "192.0.2.10"]
    by_ip = svc("lookup/host/1", "https://192.0.2.10/pscheduler")
    assert lookup.candidate_names(by_ip, h, PTR)[0] == "node10.example.org"
    assert lookup.candidate_names(by_ip, None, {}) == ["192.0.2.10"]


def test_sync_creates_refreshes_and_keeps_existing_names(app, db):
    records = [
        svc("lookup/host/1", "https://new.example.org/pscheduler"),                    # brand new
        svc("lookup/host/2", "https://192.0.2.20/pscheduler", tests=("latencybg",)),   # known by its PTR name
        svc("lookup/host/3", "https://a.example.org/pscheduler", tests=("throughput", "dns")),
    ]
    hosts = [host("lookup/host/1", ["192.0.2.10"], site="New Site"),
             host("lookup/host/2", ["192.0.2.20"]),
             host("lookup/host/3", ["192.0.2.30", "a.example.org"], site="Renamed A", communities=("WLCG", "OSG"))]
    db.add(m.Host(hostname="old-name.example.org", source="manual"))
    db.commit()
    now = datetime.datetime(2026, 11, 20, 12, 0)
    summary = lookup.sync(db, records, hosts, now=now, resolve=resolve)
    assert summary["created"] == ["new.example.org"] and summary["adopted"] == ["old-name.example.org"]
    new = db.query(m.Host).filter_by(hostname="new.example.org").one()
    assert (new.site, new.source, new.last_seen, new.communities) == ("New Site", "lookup", now, ["WLCG"])
    assert new.info == {"pshost-toolkitversion": "5.2.6", "location-city": "Ann Arbor", "location-sitename": "New Site"}
    assert new.addresses == [{"address": "192.0.2.10", "family": 4}]
    a = db.query(m.Host).filter_by(hostname="a.example.org").one()
    assert a.site == "Renamed A" and a.capabilities == ["dns", "throughput"] and a.communities == ["OSG", "WLCG"]
    adopted = db.query(m.Host).filter_by(hostname="old-name.example.org").one()
    assert adopted.source == "lookup" and adopted.capabilities == ["latencybg"]
    # hosts the Lookup Service no longer lists are kept; they just stop being refreshed
    assert db.query(m.Host).filter_by(hostname="b.example.org").one().last_seen is None


def test_multi_homed_host_keeps_one_entry_per_interface(app, db):
    shared = host("lookup/host/9", ["192.0.2.10"])
    records = [svc("lookup/host/9", "https://ps-a.example.org/pscheduler"),
               svc("lookup/host/9", "https://ps-b.example.org/pscheduler")]
    summary = lookup.sync(db, records, [shared], resolve=resolve)
    assert summary["created"] == ["ps-a.example.org", "ps-b.example.org"]


def test_rule_based_group_picks_up_new_hosts_on_sync(app, db):
    group = m.HostGroup(name="All OSG throughput", rule={"match": "all", "conditions": [
        {"field": "communities", "op": "is", "value": "OSG"},
        {"field": "capabilities", "op": "is", "value": "throughput"}]})
    db.add(group)
    db.commit()
    assert [h.hostname for h in services.Membership(db).of(group)] == ["d.example.org"]
    lookup.sync(db, [svc("lookup/host/1", "https://new.example.org/pscheduler")],
                [host("lookup/host/1", ["192.0.2.10"], communities=("OSG",))], resolve=resolve)
    assert [h.hostname for h in services.Membership(db).of(group)] == ["d.example.org", "new.example.org"]


def test_dry_run_changes_nothing(app, db):
    before = db.query(m.Host).count()
    summary = lookup.sync(db, [svc("lookup/host/1", "https://new.example.org/pscheduler")], [], dry_run=True,
                          resolve=resolve)
    assert summary["created"] == ["new.example.org"] and db.query(m.Host).count() == before
