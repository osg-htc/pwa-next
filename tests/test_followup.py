import csv
import datetime
import io

from pwa import followup, mcp, models as m


def _silent(db):
    a = db.query(m.Host).filter_by(hostname="a.example.org").one()
    a.last_seen = datetime.datetime.utcnow() - datetime.timedelta(days=400)
    a.info = {"pshost-toolkitversion": "5.0.8"}
    db.commit()


def test_record_a_step_and_see_it(app, db, as_user):
    _silent(db)
    admin = as_user("admin")
    page = admin.get("/reports/follow-up").get_data(as_text=True)
    assert "not contacted yet" in page and "Record a step" in page
    r = admin.post("/reports/follow-up/contacts", {"site": "Site A", "status": "asked", "via": "ticket",
                                                   "reference": "GGUS 123456", "note": "please update to 5.2",
                                                   "contacted": "2026-11-20", "hostname": "a.example.org"})
    assert r.status_code == 302 and r.headers["Location"].endswith("#site-Site-A")
    entry = db.query(m.SiteContact).one()
    assert (entry.site, entry.status, entry.via, entry.reference, entry.hostname, entry.created_by) == \
        ("Site A", "asked", "ticket", "GGUS 123456", "a.example.org", "Ada Admin")
    assert entry.contacted == datetime.date(2026, 11, 20)
    page = admin.get("/reports/follow-up").get_data(as_text=True)
    assert "Asked the site" in page and "GGUS 123456" in page and "please update to 5.2" in page
    rows = list(csv.DictReader(io.StringIO(admin.get("/reports/follow-up.csv").get_data(as_text=True))))
    assert rows[0]["status"] == "Asked the site" and rows[0]["last contacted"] == "2026-11-20"
    assert db.query(m.AuditLog).filter_by(action="contact-site").one().object_label == "Site A"
    admin.post("/reports/follow-up/contacts", {"site": "Site A", "status": "fixed"})   # newest entry wins
    assert "Site says it is fixed" in admin.get("/reports/follow-up").get_data(as_text=True)
    assert db.query(m.SiteContact).count() == 2


def test_who_may_record_and_delete(app, db, as_user, give):
    _silent(db)
    alice, bob = as_user("alice"), as_user("bob")
    assert "Record a step" not in alice.get("/reports/follow-up").get_data(as_text=True)
    assert alice.post("/reports/follow-up/contacts", {"site": "Site A", "status": "asked"}).status_code == 403
    give("alice", "editor", "mesh", app.ids["mesh"])          # a.example.org is in that mesh
    assert alice.post("/reports/follow-up/contacts", {"site": "Site A", "status": "asked"}).status_code == 302
    entry = db.query(m.SiteContact).one()
    assert bob.post("/reports/follow-up/contacts/%d/delete" % entry.id).status_code == 403
    assert alice.post("/reports/follow-up/contacts/%d/delete" % entry.id).status_code == 302
    db.expire_all()
    assert db.query(m.SiteContact).count() == 0
    assert db.query(m.AuditLog).filter_by(action="delete-contact").count() == 1


def test_bad_entries_are_refused(app, db, as_user):
    _silent(db)
    admin = as_user("admin")
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    for form in ({"site": "Site A", "status": "bored"}, {"site": "Site A", "status": "asked", "contacted": tomorrow},
                 {"site": "Site A", "status": "asked", "contacted": "26/11/2026"},
                 {"site": "Site A", "status": "asked", "via": "pigeon"}):
        admin.post("/reports/follow-up/contacts", form)
    assert db.query(m.SiteContact).count() == 0
    assert followup.anchor("RNP/RedClara-Sao (BR)") == "site-RNP-RedClara-Sao-BR"


def test_mcp_records_follow_up(app, db):
    _silent(db)
    admin = mcp.Server(app.extensions["pwa_session"], dict(app.config), "u:admin", allow_changes=True)
    bob = mcp.Server(app.extensions["pwa_session"], dict(app.config), "u:bob", allow_changes=True)

    def call(srv, _tool, **args):
        return srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": _tool, "arguments": args}})["result"]
    r = call(bob, "record_site_contact", site="Site A", status="asked")
    assert r["isError"] and "may not" in r["structuredContent"]["error"]
    assert call(bob, "record_site_contact", site="Nowhere", status="asked")["isError"]
    r = call(admin, "record_site_contact", site="site a", status="replied", via="e-mail", note="will reinstall")
    assert not r["isError"] and r["structuredContent"]["site"] == "Site A"
    assert db.query(m.SiteContact).one().created_by == "Ada Admin (via MCP)"
    report = call(admin, "report", name="follow_up_by_site")["structuredContent"]
    assert report["sites"][0]["status"] == "Site replied"
    assert report["sites"][0]["follow_up"][0]["note"] == "will reinstall"
