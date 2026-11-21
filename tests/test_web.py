import json
import os

from pwa import models as m


def published(app, rel):
    with open(os.path.join(app.config["PUBLISH_DIR"], rel)) as f:
        return json.load(f)


def test_every_page_renders(app, as_user):
    b, ids = as_user("admin"), app.ids
    for url in ["/", "/meshes", "/meshes/new", "/meshes/%d" % ids["mesh"], "/meshes/%d/preview" % ids["mesh"],
                "/meshes/%d/tasks/new" % ids["mesh"], "/meshes/%d/tasks/%d" % (ids["mesh"], ids["task"]),
                "/groups", "/groups/new", "/groups/%d" % ids["g1"], "/tests", "/tests/new?type=dns",
                "/tests/new?type=snmpget", "/tests/%d" % ids["test"], "/hosts", "/hosts?show=all&q=exa",
                "/hosts/new", "/hosts/%d" % ids["hosts"]["a"], "/hosts/%d/remove" % ids["hosts"]["a"],
                "/archives", "/archives/new?type=http", "/archives/%d" % ids["archive"], "/access",
                "/access/users/%d" % ids["users"]["alice"], "/audit", "/pub/config", "/pub/config/demo",
                "/pub/auto/a.example.org"]:
        assert b.get(url).status_code == 200, url
    assert b.get("/pub/auto/unknown.example.org").status_code == 404


def test_published_files_need_no_login(app):
    c = app.test_client()
    assert c.get("/pub/config/demo").status_code == 200
    assert c.get("/meshes").status_code == 302          # everything else asks you to sign in


def test_type_ahead_when_adding_hosts(app, as_user):
    b = as_user("admin")
    page = b.get("/groups/%d/suggest?_q=exam" % app.ids["g1"]).data.decode()
    # a and b are already members; d offers throughput so it is listed before c and e
    assert "a.example.org" not in page and "b.example.org" not in page
    assert page.index("d.example.org") < page.index("c.example.org")
    assert "does not advertise throughput" in page
    # several words narrow the match; site names match too
    assert "d.example.org" in b.get("/groups/%d/suggest?_q=site+d" % app.ids["g1"]).data.decode()
    assert "No host matches" in b.get("/groups/%d/suggest?_q=zzz" % app.ids["g1"]).data.decode()


def test_adding_a_host_publishes_it(app, as_user):
    b = as_user("admin")
    r = b.post("/groups/%d/members" % app.ids["g1"], {"host_id": app.ids["hosts"]["d"]}, htmx=True)
    assert r.status_code == 200 and b"d.example.org" in r.data and r.headers["HX-Trigger"] == "members-changed"
    assert "d.example.org" in published(app, "config/demo")["addresses"]
    assert list(published(app, "auto/d.example.org")["tasks"]) == ["Demo throughput"]


def test_bulk_add_reports_unknown_hosts(app, as_user):
    b = as_user("admin")
    r = b.post("/groups/%d/members/bulk" % app.ids["g1"],
               {"hostnames": "d.example.org\nnope.example.org, e.example.org"}, follow_redirects=True)
    assert b"Added 2 host(s)" in r.data and b"nope.example.org" in r.data


def test_remove_host_from_everything(app, as_user, db):
    b = as_user("admin")
    preview = b.get("/hosts/%d/remove" % app.ids["hosts"]["a"]).data.decode()
    assert "Throughput hosts" in preview and "Demo mesh" in preview
    r = b.post("/hosts/%d/remove" % app.ids["hosts"]["a"], follow_redirects=True)
    assert b"Removed a.example.org from 1 host group(s)" in r.data
    assert set(published(app, "config/demo")["addresses"]) == {"b.example.org"}
    assert not os.path.exists(os.path.join(app.config["PUBLISH_DIR"], "auto/a.example.org"))
    entry = db.query(m.AuditLog).filter_by(action="remove-host").one()
    assert entry.actor == "Ada Admin" and entry.detail["groups"] == ["Throughput hosts"]


def test_test_form_has_dropdowns_and_rejects_bad_values(app, as_user, db):
    b = as_user("admin")
    page = b.get("/tests/%d" % app.ids["test"]).data.decode()
    assert '<select id="spec.ip-version" name="spec.ip-version">' in page
    assert '<option value="4" selected>4</option>' in page
    form = {"name": "Six-hour throughput", "runs": "repeat", "repeat": "6h", "tools": "iperf3",
            "spec.duration": "30s", "spec.ip-version": "6", "spec.parallel": "0"}
    r = b.post("/tests/%d" % app.ids["test"], form)
    assert r.status_code == 400 and b"Not saved" in r.data and b"parallel" in r.data
    assert db.get(m.Test, app.ids["test"]).spec == {"duration": "PT25S", "ip-version": 4}     # untouched
    form["spec.parallel"] = "4"
    r = b.post("/tests/%d" % app.ids["test"], form, follow_redirects=True)
    assert b"1 task(s) republished" in r.data
    spec = published(app, "config/demo")["tests"]["Demo throughput"]["spec"]
    assert spec["duration"] == "PT30S" and spec["ip-version"] == 6 and spec["parallel"] == 4


def test_ipv6_choice_survives_a_round_trip(app, as_user):
    """PWA issue #217: an IPv6-only test showed as IPv4-only when reopened."""
    b = as_user("admin")
    b.post("/tests/%d" % app.ids["test"], {"name": "Six-hour throughput", "runs": "repeat", "repeat": "PT6H",
                                           "spec.duration": "PT25S", "spec.ip-version": "6"})
    page = b.get("/tests/%d" % app.ids["test"]).data.decode()
    assert '<option value="6" selected>6</option>' in page and '<option value="4" selected' not in page


def test_new_test_of_a_type_pwa_never_had(app, as_user, db):
    b = as_user("admin")
    r = b.post("/tests/new?type=dns", {"name": "Resolve example", "runs": "repeat", "repeat": "5m",
                                       "spec.query": "example.org", "spec.record": "a"})
    assert r.status_code == 302
    t = db.query(m.Test).filter_by(name="Resolve example").one()
    assert (t.type, t.repeat, t.slip, t.spec) == ("dns", "PT5M", "PT2M30S", {"query": "example.org", "record": "a"})


def test_task_editor_uses_pickers(app, as_user, db):
    b = as_user("admin")
    assert b"Six-hour throughput" in b.get("/suggest/test?_q=six+thr").data
    assert b"Latency hosts" in b.get("/suggest/hostgroup?_q=lat").data and b"1 hosts" in b.get("/suggest/hostgroup?_q=lat").data
    url = "/meshes/%d/tasks/new" % app.ids["mesh"]
    r = b.post(url, {"name": "Across", "topology": "disjoint", "test_id": app.ids["test"],
                     "group_a_id": app.ids["g1"], "enabled": "on"})
    assert r.status_code == 400 and b"second host group" in r.data
    r = b.post(url, {"name": "Across", "topology": "disjoint", "test_id": app.ids["test"],
                     "group_a_id": app.ids["g1"], "group_b_id": app.ids["g2"], "enabled": "on"})
    assert r.status_code == 302
    assert published(app, "config/demo")["groups"]["Across"]["type"] == "disjoint"
    assert "c.example.org" in published(app, "config/demo")["addresses"]


def test_cannot_delete_what_is_in_use(app, as_user, db):
    b = as_user("admin")
    r = b.post("/groups/%d/delete" % app.ids["g1"], follow_redirects=True)
    assert b"still in use" in r.data and db.get(m.HostGroup, app.ids["g1"]) is not None
    r = b.post("/groups/%d/delete" % app.ids["g2"], follow_redirects=True)
    assert b"Deleted" in r.data


def test_mesh_copy_starts_disabled(app, as_user, db):
    b = as_user("admin")
    b.post("/meshes/%d/clone" % app.ids["mesh"])
    copy = db.query(m.Mesh).filter_by(slug="demo-copy").one()
    assert [t.enabled for t in copy.tasks] == [False]
    assert published(app, "config/demo-copy")["tasks"] == {}


def test_needs_attention_links_to_the_mesh_and_the_test(app, as_user, db):
    mesh = db.get(m.Mesh, app.ids["mesh"])
    bad = m.Test(name="Bad latency", type="latency", spec={"bucket-width": 1}, repeat="PT1M")
    mesh.tasks.append(m.MeshTask(position=1, name="Bad task", test=bad, group_a=db.get(m.HostGroup, app.ids["g1"])))
    db.commit()
    b = as_user("admin")
    b.post("/publish")
    page = b.get("/").data.decode()
    assert 'href="/meshes/%d"' % app.ids["mesh"] in page and 'href="/tests/%d"' % bad.id in page
    assert "Bad task" in page and "bucket-width" in page
    # published files are linked on this server's own /pub path, whatever PWA_PUBLIC_URL says
    assert 'href="/pub/config/demo"' in b.get("/meshes").data.decode()
    assert b.get("/pub/config/demo").status_code == 200
