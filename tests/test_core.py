import json
import os

from pwa import importer, models as m, publish, services, specforms


def published(app, rel):
    with open(os.path.join(app.config["PUBLISH_DIR"], rel)) as f:
        return json.load(f)


# -- forms from pScheduler's schemas ---------------------------------------

def test_fields_come_from_the_schema_with_dropdowns():
    fields = {f["name"]: f for f in specforms.fields("tests", "throughput")}
    assert fields["ip-version"]["kind"] == "select" and fields["ip-version"]["choices"] == [4, 6]
    assert fields["duration"]["kind"] == "duration"
    assert fields["udp"]["kind"] == "bool"
    assert fields["parallel"]["kind"] == "number" and fields["parallel"]["minimum"] == 1
    assert "source" not in fields and "dest" not in fields and "schema" not in fields
    assert {f["name"] for f in specforms.fields("tests", "trace")} >= {"probe-type", "first-ttl"}
    assert specforms.fields("tests", "dns")            # types PWA never supported
    assert specforms.fields("archivers", "http")


def test_validation_and_schema_version():
    assert specforms.validate("tests", "throughput", {"duration": "PT25S", "ip-version": 6}) == []
    assert "must be less than 1" in specforms.validate("tests", "latency", {"bucket-width": 1})[0]
    assert "is not an option of rtt tests" in specforms.validate("tests", "rtt", {"bogus": 1})[0]
    assert "duration" in specforms.validate("tests", "throughput", {"duration": "25"})[0]
    assert specforms.schema_version("tests", "throughput", {"duration": "PT25S"}) == 1
    assert specforms.schema_version("tests", "throughput", {"single-ended": True}) == 2
    assert specforms.schema_version("tests", "rtt", {"fragment": True}) == 3


def test_durations():
    assert specforms.normalize_duration("6h") == "PT6H"
    assert specforms.normalize_duration("90") == "PT1M30S"
    assert specforms.normalize_duration("pt30s") == "PT30S"
    assert specforms.seconds_to_iso(86400) == "P1D"
    assert specforms.iso_to_seconds("PT6H") == 21600


# -- translation of PWA test specs ------------------------------------------

def test_translate_throughput():
    t = importer.translate_testspec("bwctl", {
        "tool": "bwctl/iperf3", "protocol": "tcp", "interval": 21600, "duration": 25,
        "random_start_percentage": 10, "omit_interval": 5, "flow_label": 32776, "ipv6_only": True},
        "interval")
    assert t["type"] == "throughput"
    assert t["spec"] == {"duration": "PT25S", "omit": "PT5S", "flow-label": 32776, "ip-version": 6}
    assert (t["repeat"], t["slip"], t["tools"]) == ("PT6H", "PT3H", ["iperf3"])


def test_translate_latency_kinds():
    spec = {"tool": "owping", "packet_interval": 0.1, "sample_count": 600, "bucket_width": 0.001}
    continuous = importer.translate_testspec("owamp", dict(spec), None)
    assert continuous["type"] == "latencybg" and continuous["repeat"] is None and continuous["tools"] == []
    assert continuous["spec"] == {"packet-interval": 0.1, "packet-count": 600, "bucket-width": 0.001}
    scheduled = importer.translate_testspec("owamp", dict(spec, interval=60, tool="twping"), "interval")
    assert scheduled["type"] == "latency" and scheduled["repeat"] == "PT1M" and scheduled["tools"] == ["twping"]


def test_translate_ping_and_trace():
    ping = importer.translate_testspec("ping", {"interval": 300, "packet_count": 10, "packet_interval": 1,
                                                "packet_size": 1000}, "interval")
    assert ping["spec"] == {"count": 10, "interval": "PT1S", "length": 1000} and ping["repeat"] == "PT5M"
    trace = importer.translate_testspec("traceroute", {"interval": 600, "protocol": "icmp", "probe_type": "",
                                                       "first_ttl": "1", "timeout": 5, "ipv4_only": True}, None)
    assert trace["type"] == "trace"
    assert trace["spec"] == {"first-ttl": 1, "wait": "PT5S", "ip-version": 4}


# -- publishing --------------------------------------------------------------

def test_published_template(app, db):
    t = published(app, "config/demo")
    assert publish.psconfig_schema().is_valid(t)
    assert set(t["addresses"]) == {"a.example.org", "b.example.org"}
    assert t["groups"]["Demo throughput"] == {"type": "mesh", "addresses": [
        {"name": "a.example.org"}, {"name": "b.example.org"}]}
    spec = t["tests"]["Demo throughput"]["spec"]
    assert spec == {"duration": "PT25S", "ip-version": 4, "source": "{% address[0] %}",
                    "dest": "{% address[1] %}", "schema": 1}
    task = t["tasks"]["Demo throughput"]
    assert t["schedules"][task["schedule"]] == {"repeat": "PT6H", "sliprand": True, "slip": "PT3H"}
    assert task["tools"] == ["iperf3"]
    assert [t["archives"][a]["archiver"] for a in task["archives"]] == ["http"]
    # what the 5.x Grafana agent needs to name grids and dashboards (PWA issue #229)
    assert task["reference"] == {"display-task-name": "Demo throughput", "display-task-group": ["Demo mesh"]}
    assert published(app, "index.json") == [{"include": ["https://ps.example.org/pub/config/demo"]}]


def test_per_host_feed(app, db):
    feed = published(app, "auto/a.example.org")
    assert list(feed["tasks"]) == ["Demo throughput"]
    assert set(feed["addresses"]) == {"a.example.org", "b.example.org"}
    # A known host with no tests still gets a valid, empty template rather than a 404.
    empty = published(app, "auto/e.example.org")
    assert empty["tasks"] == {} and publish.psconfig_schema().is_valid(empty)


def test_disjoint_and_same_task_name_in_two_meshes(app, db):
    g1, g2 = db.get(m.HostGroup, app.ids["g1"]), db.get(m.HostGroup, app.ids["g2"])
    other = m.Mesh(slug="other", name="Other")
    other.tasks = [m.MeshTask(name="Demo throughput", topology="disjoint", test=db.get(m.Test, app.ids["test"]),
                              group_a=g1, group_b=g2)]
    db.add(other)
    db.commit()
    publish.publish_all(db, app.config["PUBLISH_DIR"], app.config["PUBLIC_URL"])
    assert published(app, "config/other")["groups"]["Demo throughput"] == {
        "type": "disjoint", "a-addresses": [{"name": "a.example.org"}, {"name": "b.example.org"}],
        "b-addresses": [{"name": "c.example.org"}]}
    # PWA merged same-named tasks from different meshes in a host's feed; here both survive.
    assert set(published(app, "auto/a.example.org")["tasks"]) == {
        "Demo throughput [demo]", "Demo throughput [other]"}


def test_invalid_test_is_withheld_not_published(app, db):
    mesh = db.get(m.Mesh, app.ids["mesh"])
    bad = m.Test(name="Bad latency", type="latency", spec={"bucket-width": 1}, repeat="PT1M")
    mesh.tasks.append(m.MeshTask(position=1, name="Bad task", test=bad, group_a=db.get(m.HostGroup, app.ids["g1"])))
    db.commit()
    result = publish.publish_all(db, app.config["PUBLISH_DIR"], app.config["PUBLIC_URL"])
    assert result["failed"] == [] and result["withheld"][0][:2] == ("demo", "Bad task")
    assert list(published(app, "config/demo")["tasks"]) == ["Demo throughput"]     # the good task still flows
    assert "Bad task" in db.get(m.Publication, "config/demo").message


def test_unchanged_files_are_not_rewritten(app, db):
    result = publish.publish_all(db, app.config["PUBLISH_DIR"], app.config["PUBLIC_URL"])
    assert result["written"] == 0 and result["unchanged"] == 6          # 1 mesh file + 5 per-host feeds
    db.delete(db.get(m.Mesh, app.ids["mesh"]))
    db.commit()
    result = publish.publish_all(db, app.config["PUBLISH_DIR"], app.config["PUBLIC_URL"])
    assert result["removed"] == 1 and not os.path.exists(os.path.join(app.config["PUBLISH_DIR"], "config/demo"))


# -- membership ---------------------------------------------------------------

def test_nested_groups_rules_and_excludes(app, db):
    g1, g2 = db.get(m.HostGroup, app.ids["g1"]), db.get(m.HostGroup, app.ids["g2"])
    hosts = {h.hostname[0]: h for h in db.query(m.Host)}
    everyone = m.HostGroup(name="Everyone", includes=[g1, g2],
                           rule={"match": "all", "conditions": [{"field": "communities", "op": "is", "value": "osg"}]},
                           excludes=[hosts["b"]])
    db.add(everyone)
    db.commit()
    mem = services.Membership(db)
    assert [h.hostname[0] for h in mem.of(everyone)] == ["a", "c", "d"]      # b excluded, d by rule
    why = mem.why(everyone)
    assert why[hosts["d"].id] == ["matches the rule"] and "via group" in why[hosts["a"].id][0]
    # a group added to an included group shows up without touching "Everyone"
    g2.members.append(hosts["e"])
    db.commit()
    assert "e" in [h.hostname[0] for h in services.Membership(db).of(everyone)]
    assert services.would_loop(g1, everyone) and not services.would_loop(everyone, g1)


def test_remove_host_cascades(app, db):
    host = db.get(m.Host, app.ids["hosts"]["a"])
    latency = db.get(m.HostGroup, app.ids["g2"])
    latency.members.append(host)
    db.commit()
    summary = services.remove_host(db, host, "tester")
    db.commit()
    assert sorted(summary["groups"]) == ["Latency hosts", "Throughput hosts"] and summary["meshes"] == ["Demo mesh"]
    assert db.query(m.Host).filter_by(hostname="a.example.org").count() == 0
    assert [h.hostname for h in db.get(m.HostGroup, app.ids["g1"]).members] == ["b.example.org"]


def test_import_users_from_the_old_auth_service(tmp_path, db):
    export = tmp_path / "export"
    export.mkdir()
    oid = lambda n: {"$oid": "%024x" % n}
    docs = {
        "hosts": [{"_id": oid(1), "hostname": "h1.example.net", "lsid": "gls", "sitename": "LS name",
                   "desc": "Hand-set name", "services": [{"type": "bwctl"}], "admins": []}],
        "hostgroups": [{"_id": oid(2), "name": "G", "service_type": "bwctl", "hosts": [oid(1)], "admins": ["3", "4", None]}],
        "testspecs": [{"_id": oid(3), "name": "T", "service_type": "bwctl", "schedule_type": "interval",
                       "specs": {"interval": 3600, "duration": 20, "tool": "bwctl/iperf3"}, "admins": ["3"]}],
        "archives": [],
        "configs": [{"_id": oid(4), "url": "imported", "name": "Imported", "admins": ["4"], "archives": [],
                     "tests": [{"name": "Task", "service_type": "bwctl", "mesh_type": "mesh", "agroup": oid(2),
                                "testspec": oid(3), "enabled": True}]}],
        "users": [{"id": 3, "fullname": "Known Login", "email": "k@example.net", "admin": False, "active": True,
                   "subs": ["http://cilogon.org/serverA/users/3", "http://cilogon.org/serverB/users/33"]},
                  {"id": 4, "fullname": "Password Only", "email": "p@example.net", "subs": [], "active": True},
                  {"id": 9, "fullname": "Old Admin", "email": "a@example.net", "subs": [], "admin": True}],
    }
    for name, content in docs.items():
        (export / (name + ".json")).write_text(json.dumps(content))
    for model in (m.MeshTask, m.Mesh, m.Test, m.HostGroup, m.Host, m.Archive, m.User):
        for row in db.query(model):
            db.delete(row)
    db.commit()
    report = importer.import_pwa(db, str(export))
    assert report.counts["users"] == 3 and report.counts["users with a known federated login"] == 1
    people = {u.name: u for u in db.query(m.User)}
    known = people["Known Login"]
    assert known.subject == "http://cilogon.org/serverA/users/3" and not known.is_placeholder
    assert [i.subject for i in known.identities] == ["http://cilogon.org/serverB/users/33"]
    assert people["Password Only"].is_placeholder and people["Password Only"].email == "p@example.net"
    assert people["Old Admin"].is_admin and people["Old Admin"].is_placeholder
    host = db.query(m.Host).one()
    assert (host.display_name, host.site, host.shown_name) == ("Hand-set name", "LS name", "Hand-set name")
    grants = {(g.principal_id, g.scope_type) for g in db.query(m.Grant)}
    assert grants == {(known.id, "hostgroup"), (people["Password Only"].id, "hostgroup"),
                      (known.id, "test"), (people["Password Only"].id, "mesh")}


def test_data_files_read_without_a_utf8_locale():
    """Apache runs the application with an ASCII locale; data files must not depend on it."""
    import subprocess
    import sys
    code = ("from pwa import testinfo, specforms, publish; "
            "testinfo.guidance('throughput'); specforms.fields('tests', 'throughput'); publish.psconfig_schema()")
    env = {"LANG": "C", "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii",
           "PATH": os.environ.get("PATH", "")}
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    r = subprocess.run([sys.executable, "-X", "utf8=0", "-c", code], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]


def test_translate_pwa_streams_bug():
    t = importer.translate_testspec("bwctl", {"interval": 21600, "duration": 25, "streams": 5}, "interval")
    assert t["spec"] == {"duration": "PT25S", "parallel": 5} and "PWA issue #234" in t["notes"][0]
