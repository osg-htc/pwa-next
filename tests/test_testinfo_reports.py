import json
import os

from pwa import models as m, publish, reports, testinfo


def published(app, rel):
    with open(os.path.join(app.config["PUBLISH_DIR"], rel)) as f:
        return json.load(f)


# -- what tools support ---------------------------------------------------------

def test_catalog_knows_tool_support():
    assert testinfo.tools("throughput")[0] == "iperf3"
    assert testinfo.option_tools("throughput", "zero-copy") == ["iperf3"]
    assert testinfo.option_note("throughput", "zero-copy") == "only iperf3"
    assert testinfo.option_note("throughput", "duration") == ""
    assert testinfo.option_value_tools("rtt", "protocol")["tcp"] == ["tcpping"]
    assert testinfo.guidance("netreach")["availability"] == "extras"


def test_problems_with_tools_and_options():
    assert testinfo.problems("throughput", {"duration": "PT20S", "omit": "PT5S"}, ["iperf3"]) == []
    assert "works only with" in testinfo.problems("throughput", {"omit": "PT5S"}, ["nuttcp"])[0]
    assert "No single tool" in testinfo.problems("throughput", {"omit": "PT5S", "burst-size": 2}, [])[0]
    assert "twping" in testinfo.problems("latency", {"protocol": "twamp"}, ["owping"])[0]


def test_editor_refuses_option_the_chosen_tool_lacks(app, as_user, db):
    b = as_user("admin")
    form = {"name": "Six-hour throughput", "runs": "repeat", "repeat": "PT6H", "tools": "nuttcp",
            "spec.duration": "PT20S", "spec.zero-copy": "on"}
    r = b.post("/tests/%d" % app.ids["test"], form)
    assert r.status_code == 400 and b"works only with iperf3" in r.data
    page = b.get("/tests/%d" % app.ids["test"]).data.decode()
    assert "only iperf3" in page and "What it does, when to use it" in page


# -- single-host test types -----------------------------------------------------

def test_single_host_tests_publish_as_lists(app, as_user, db):
    b = as_user("admin")
    r = b.post("/tests/new?type=dns", {"name": "Resolve", "runs": "repeat", "repeat": "1h",
                                       "spec.query": "example.org", "spec.record": "a"})
    assert r.status_code == 302
    dns = db.query(m.Test).filter_by(name="Resolve").one()
    r = b.post("/meshes/%d/tasks/new" % app.ids["mesh"], {"name": "DNS everywhere", "topology": "disjoint",
                                                          "test_id": dns.id, "group_a_id": app.ids["g1"],
                                                          "group_b_id": app.ids["g2"], "enabled": "on"})
    assert r.status_code == 302
    t = published(app, "config/demo")
    assert t["groups"]["DNS everywhere"] == {"type": "list", "addresses": [
        {"name": "a.example.org"}, {"name": "b.example.org"}]}          # one group is all it uses
    assert t["tests"]["DNS everywhere"]["spec"]["host"] == "{% address[0] %}"
    assert "source" not in t["tests"]["DNS everywhere"]["spec"]
    assert publish.psconfig_schema().is_valid(t)


def test_tests_that_cannot_be_addressed_by_host_are_refused(app, as_user, db):
    snmp = m.Test(name="SNMP", type="snmpget", spec={}, repeat="PT5M")
    db.add(snmp)
    db.commit()
    r = as_user("admin").post("/meshes/%d/tasks/new" % app.ids["mesh"], {
        "name": "Poll", "topology": "mesh", "test_id": snmp.id, "group_a_id": app.ids["g1"], "enabled": "on"})
    assert r.status_code == 400 and b"not addressed by host" in r.data


# -- pages ------------------------------------------------------------------------

def test_test_type_pages(app, as_user):
    b = as_user("admin")
    page = b.get("/test-types").data.decode()
    assert "Network reachability" in page and "extras bundle" in page
    page = b.get("/test-types/throughput").data.decode()
    assert "zero-copy" in page and "nuttcp" in page and "first choice" in page
    assert "Determine if any host on a network is reachable" not in page
    assert b"is reachable" in b.get("/test-types/netreach").data
    assert b.get("/test-types/no-such-type").status_code == 404


def test_tests_filters(app, as_user, db):
    db.add(m.Test(name="Unused latency", type="latencybg", spec={"ip-version": 6}, repeat=None))
    db.commit()
    b = as_user("admin")
    count = lambda q: b.get("/tests" + q).data.decode().count('href="/tests/')
    assert count("") == 2
    assert count("?q=six") == 1 and count("?q=unused+latency") == 1
    assert count("?type=latencybg") == 1 and count("?runs=continuous") == 1 and count("?runs=PT6H") == 1
    assert count("?state=used") == 1 and count("?state=unused") == 1
    assert count("?ipv=6") == 1 and count("?ipv=4") == 1
    assert count("?tool=iperf3") == 1 and count("?mesh=%d" % app.ids["mesh"]) == 1
    r = b.get("/tests?q=six", headers={"HX-Request": "true"})
    assert b"<html" not in r.data and b"1 of 2 tests" in r.data


# -- reports --------------------------------------------------------------------------

def test_reports(app, as_user, db):
    g1 = db.get(m.HostGroup, app.ids["g1"])
    test = db.get(m.Test, app.ids["test"])
    other = m.Mesh(slug="other", name="Other")
    other.tasks = [m.MeshTask(name="Same again", topology="mesh", test=test, group_a=g1)]
    db.add(other)
    db.commit()
    all_tasks = reports.tasks(db)
    load = {e["host"].hostname: e for e in reports.host_load(all_tasks)}
    a = load["a.example.org"]
    # 2 meshes x 2 directions x 4 runs a day, 25 seconds each
    assert a["throughput_runs"] == 16 and a["throughput_seconds"] == 400 and a["partners"] == 1
    over = reports.overlaps(all_tasks)
    assert over[0]["pairs"] == 2 and over[0]["same_settings"]
    summary = {s["mesh"].slug: s for s in reports.mesh_summary(all_tasks)}
    assert summary["demo"]["measurements"] == 2 and round(summary["demo"]["throughput_hours"], 3) == round(2 * 4 * 25 / 3600, 3)
    rows = reports.between(all_tasks, db.query(m.Host).filter_by(hostname="a.example.org").one(),
                           db.query(m.Host).filter_by(hostname="b.example.org").one())
    assert {r["direction"] for r in rows} == {"both directions"} and len(rows) == 2
    b = as_user("admin")
    page = b.get("/reports").data.decode()
    assert "Measurements made more than once" in page and "same test" in page
    assert b.get("/reports/between?a=%d&b=%d" % (app.ids["hosts"]["a"], app.ids["hosts"]["b"])).status_code == 200
