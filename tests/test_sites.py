import csv
import datetime
import io
import json

from pwa import mcp, models as m, reports, services, sites

CRIC_SERVICES = json.dumps({
    "SITE-A-PerfSonar-Bandwidth-a.example.org": {
        "endpoint": "a.example.org", "rcsite": "SITE-A", "federation": "XX-FED", "flavour": "Bandwidth",
        "state": "ACTIVE", "status": "production", "usage": {"atlas": [{}], "cms": [{}]}},
    "SITE-Q-PerfSonar-Latency-q.example.org": {      # a second site in example.org: no domain guess there
        "endpoint": "q.example.org", "rcsite": "SITE-Q", "flavour": "Latency", "state": "ACTIVE",
        "status": "production", "usage": {}},
    "SITE-Z-PerfSonar-Latency-ps.zeta.ac.uk": {
        "endpoint": "ps.zeta.ac.uk", "rcsite": "SITE-Z", "flavour": "Latency", "state": "ACTIVE",
        "status": "DISABLED", "usage": {}},
}).encode()
CRIC_SITES = json.dumps({
    "SITE-A": {"rc_tier_level": 2, "country": "Freedonia", "federations": [{"name": "XX-FED"}], "oim_groupid": None},
    "SITE-Z": {"rc_tier_level": 1, "country": "Zembla", "federations": [], "oim_groupid": None},
    "SITE-B": {"rc_tier_level": 3, "country": "Freedonia", "federations": [], "oim_groupid": None},
}).encode()
GOCDB = (b'<?xml version="1.0"?><results><SITE ID="42" NAME="SITE-A" ROC="NGI_XX" COUNTRY="Freedonia"/>'
         b'<SITE ID="43" NAME="SITE-Z" ROC="NGI_ZZ" COUNTRY="Zembla"/></results>')
OSG = b"""<ResourceSummary><ResourceGroup><GroupName>Grp</GroupName><Site><Name>OSG Site D</Name></Site>
<Facility><Name>Uni D</Name></Facility><Resources><Resource><Name>D-PS</Name><FQDN>d.example.org</FQDN>
<Services><Service><Name>net.perfSONAR.Bandwidth</Name></Service></Services>
<ContactLists><ContactList><ContactType>Administrative Contact</ContactType><Contacts>
<Contact><Name>Dana Admin</Name></Contact></Contacts></ContactList></ContactLists>
</Resource></Resources></ResourceGroup></ResourceSummary>"""
SOURCES = {sites.CRIC_SERVICES: CRIC_SERVICES, sites.CRIC_SITES: CRIC_SITES, sites.GOCDB_SITES: GOCDB,
           sites.OSG_TOPOLOGY: OSG}


def fake_fetch(url, ca=None):
    return SOURCES[url]


def test_parsing_and_matching_order():
    cric = sites.parse_cric_services(CRIC_SERVICES)
    built = sites.build([("a.example.org", None), ("b.example.org", "SITE-B"), ("lat.zeta.ac.uk", None),
                         ("d.example.org", None), ("nobody.example.net", "Somewhere")],
                        cric, sites.parse_cric_sites(CRIC_SITES), sites.parse_gocdb(GOCDB), sites.parse_osg(OSG))
    a = built["a.example.org"]
    assert a["matched"] == "CRIC" and a["site"] == "SITE-A" and a["cric"]["experiments"] == ["atlas", "cms"]
    assert a["gocdb"]["ngi"] == "NGI_XX" and a["gocdb"]["portal"].endswith("id=42")
    assert a["reach"] == ["Ticket to WLCG site SITE-A (GGUS); contacts in GOCDB"]
    assert built["b.example.org"]["matched"] == "site name it reports"
    z = built["lat.zeta.ac.uk"]                       # same registered domain (zeta.ac.uk) as one CRIC site
    assert z["matched"] == "CRIC site with the same domain" and z["site"] == "SITE-Z"
    d = built["d.example.org"]
    assert d["matched"] == "OSG Topology" and d["site"] == "OSG Site D"
    assert d["reach"] == ["OSG Topology administrative contact: Dana Admin"]
    assert "nobody.example.net" not in built
    assert sites.domain_of("ps.ph.ed.ac.uk") == "ed.ac.uk" and sites.domain_of("psb.cern.ch") == "cern.ch"


def test_sync_keeps_going_when_a_registry_fails(app, db):
    def flaky(url, ca=None):
        if url == sites.OSG_TOPOLOGY:
            raise OSError("down")
        return SOURCES[url]
    r = sites.sync(db, fetch=flaky)
    assert "osg" in r["errors"] and r["counts"]["cric_services"] == 3
    assert "a.example.org" in r["hosts"] and "d.example.org" not in r["hosts"]
    assert sites.registry(db)["hosts"]["a.example.org"]["site"] == "SITE-A"
    # the hourly Lookup Service refresh replaces host.info; the registry lives elsewhere
    db.query(m.Host).filter_by(hostname="a.example.org").one().info = {}
    db.commit()
    assert sites.registry(db)["hosts"]["a.example.org"]["site"] == "SITE-A"
    assert sites.sync(db, dry_run=True, fetch=fake_fetch)["hosts"]["d.example.org"]
    assert "d.example.org" not in sites.registry(db)["hosts"]           # a dry run stores nothing


def _make_silent(db):
    old = datetime.datetime.utcnow() - datetime.timedelta(days=400)
    a = db.query(m.Host).filter_by(hostname="a.example.org").one()
    a.last_seen, a.info = old, {"pshost-toolkitversion": "4.4.6"}
    b = db.query(m.Host).filter_by(hostname="b.example.org").one()
    b.source, b.last_seen, b.info = "manual", None, {}
    db.commit()


def test_follow_up_groups_by_site_with_actions(app, db):
    _make_silent(db)
    sites.sync(db, fetch=fake_fetch)
    groups = reports.follow_up(reports.tasks(db, services.Membership(db)), sites.registry(db))
    by_site = {g["site"]: g for g in groups}
    a = by_site["SITE-A"]
    assert a["tier"] == 2 and a["ngi"] == "NGI_XX" and a["experiments"] == ["atlas", "cms"]
    assert a["hosts"][0]["action"] == "reinstall" and a["hosts"][0]["silent_days"] >= 399
    assert a["hosts"][0]["cric"] == "Bandwidth production" and a["hosts"][0]["meshes"] == ["demo"]
    b = by_site["Site B"]                                 # the site name it reports, not a WLCG site here
    assert b["hosts"][0]["action"] == "never" and b["registered"] is False
    for release, action in (("5.1.4-1.el9", "update"), ("5.2.6-1.el9", "check"), (None, "unknown")):
        h = m.Host(hostname="x", source="lookup", last_seen=datetime.datetime.utcnow(),
                   info={"pshost-toolkitversion": release} if release else {})
        assert reports.action_for(h) == action


def test_follow_up_page_csv_and_mcp(app, db, as_user):
    _make_silent(db)
    page = as_user("alice").get("/reports/follow-up").get_data(as_text=True)
    assert "pwa site-sync" in page and "a.example.org" in page           # works before any sync
    sites.sync(db, fetch=fake_fetch)
    page = as_user("alice").get("/reports/follow-up").get_data(as_text=True)
    assert "SITE-A" in page and "GOCDB site page" in page and "Reinstall with perfSONAR 5.2" in page
    assert "Follow up with their sites" in as_user("alice").get("/reports").get_data(as_text=True)
    r = as_user("alice").get("/reports/follow-up.csv")
    assert r.mimetype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert {x["host"] for x in rows} == {"a.example.org", "b.example.org"}
    assert next(x for x in rows if x["host"] == "a.example.org")["ngi"] == "NGI_XX"
    srv = mcp.Server(app.extensions["pwa_session"], dict(app.config), "u:alice")
    res = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "report", "arguments": {"name": "follow_up_by_site"}}})["result"]
    data = res["structuredContent"]
    assert not res["isError"] and data["sites"][0]["hosts"][0]["ask"]
    assert {s["site"] for s in data["sites"]} == {"SITE-A", "Site B"}
