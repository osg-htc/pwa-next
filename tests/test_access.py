from pwa import authz, models as m
from pwa.web import create_app


def test_signed_in_people_can_look_but_not_change(app, as_user, db):
    alice = as_user("alice")
    assert alice.get("/meshes/%d" % app.ids["mesh"]).status_code == 200
    assert alice.post("/meshes/%d" % app.ids["mesh"], {"name": "Hijacked", "slug": "demo"}).status_code == 403
    assert alice.post("/groups/%d/members" % app.ids["g1"], {"host_id": app.ids["hosts"]["d"]}).status_code == 403
    assert alice.post("/hosts/%d/remove" % app.ids["hosts"]["a"]).status_code == 403
    assert alice.get("/tests/new").status_code == 403
    assert db.get(m.Mesh, app.ids["mesh"]).name == "Demo mesh"


def test_contributor_may_only_manage_members(app, as_user, give, db):
    give("alice", "contributor", "hostgroup", app.ids["g1"])
    alice = as_user("alice")
    assert alice.post("/groups/%d/members" % app.ids["g1"], {"host_id": app.ids["hosts"]["d"]}).status_code == 302
    assert alice.post("/groups/%d" % app.ids["g1"], {"name": "Renamed"}).status_code == 403
    assert alice.post("/groups/%d/delete" % app.ids["g1"]).status_code == 403
    assert alice.post("/groups/%d/members" % app.ids["g2"], {"host_id": app.ids["hosts"]["d"]}).status_code == 403
    assert "d.example.org" in [h.hostname for h in db.get(m.HostGroup, app.ids["g1"]).members]


def test_editor_changes_owner_also_shares_and_deletes(app, as_user, give, db):
    give("alice", "editor", "mesh", app.ids["mesh"])
    give("bob", "owner", "mesh", app.ids["mesh"])
    alice, bob = as_user("alice"), as_user("bob")
    assert alice.post("/meshes/%d" % app.ids["mesh"], {"name": "Renamed", "slug": "demo"}).status_code == 302
    share = {"scope_type": "mesh", "scope_id": app.ids["mesh"], "role": "editor",
             "principal": "user:%d" % app.ids["users"]["alice"]}
    assert alice.post("/access/grants", share).status_code == 403
    assert alice.post("/meshes/%d/delete" % app.ids["mesh"]).status_code == 403
    assert bob.post("/access/grants", share).status_code == 302
    # an owner of one mesh cannot hand out access to all meshes
    assert bob.post("/access/grants", dict(share, scope_id="")).status_code == 403
    assert bob.post("/meshes/%d/delete" % app.ids["mesh"]).status_code == 302
    assert db.get(m.Mesh, app.ids["mesh"]) is None


def test_creator_owns_what_they_create(app, as_user, give, db):
    give("alice", "creator", "hostgroup")
    alice, bob = as_user("alice"), as_user("bob")
    assert bob.post("/groups/new", {"name": "Bob's"}).status_code == 403
    assert alice.post("/groups/new", {"name": "Alice's sites"}).status_code == 302
    group = db.query(m.HostGroup).filter_by(name="Alice's sites").one()
    assert alice.post("/groups/%d" % group.id, {"name": "Alice's sites v2"}).status_code == 302
    assert bob.post("/groups/%d" % group.id, {"name": "Mine now"}).status_code == 403
    assert alice.post("/groups/%d" % app.ids["g1"], {"name": "Not mine"}).status_code == 403


def test_team_access_and_delegated_team_management(app, as_user, db):
    admin, alice, bob = as_user("admin"), as_user("alice"), as_user("bob")
    admin.post("/access/teams", {"name": "ATLAS mesh admins"})
    team = db.query(m.Team).one()
    url = "/access/teams/%d" % team.id
    admin.post(url, {"action": "add", "user_id": app.ids["users"]["alice"]})
    admin.post(url, {"action": "manager", "user_id": app.ids["users"]["alice"]})
    admin.post("/access/grants", {"scope_type": "test", "role": "editor", "principal": "team:%d" % team.id})
    edit = {"name": "Six-hour throughput", "runs": "repeat", "repeat": "PT6H", "spec.duration": "PT20S"}
    assert bob.post("/tests/%d" % app.ids["test"], edit).status_code == 403
    assert alice.post("/tests/%d" % app.ids["test"], edit).status_code == 302          # through the team
    # Alice manages the team, so she can bring Bob in without an administrator
    assert bob.post(url, {"action": "add", "user_id": app.ids["users"]["bob"]}).status_code == 403
    assert alice.post(url, {"action": "add", "user_id": app.ids["users"]["bob"]}).status_code == 302
    assert bob.post("/tests/%d" % app.ids["test"], edit).status_code == 302
    assert alice.post("/access/teams", {"name": "Another"}).status_code == 403            # creating teams: admins


def test_every_change_is_attributed(app, as_user, give, db):
    give("alice", "editor", "hostgroup", app.ids["g1"])
    as_user("alice").post("/groups/%d/members" % app.ids["g1"], {"host_id": app.ids["hosts"]["d"]})
    entry = db.query(m.AuditLog).filter_by(action="add-host").one()
    assert (entry.actor, entry.object_label, entry.detail) == ("Alice", "Throughput hosts", {"host": "d.example.org"})


def test_posts_without_the_csrf_token_are_refused(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["uid"], sess["csrf"] = app.ids["users"]["admin"], "tok"
    assert c.post("/groups/%d/delete" % app.ids["g2"]).status_code == 400
    assert c.post("/groups/%d/delete" % app.ids["g2"], data={"_csrf": "wrong"}).status_code == 400


def test_merge_imported_account_into_real_login(app, as_user, give, db):
    give("alice", "owner", "mesh", app.ids["mesh"])
    as_user("admin").post("/access/users/%d" % app.ids["users"]["alice"],
                          {"action": "merge", "target_id": app.ids["users"]["bob"]})
    assert db.get(m.User, app.ids["users"]["alice"]) is None
    assert as_user("bob").post("/meshes/%d" % app.ids["mesh"], {"name": "Bob's now", "slug": "demo"}).status_code == 302


def test_administrator_deletes_an_account(app, as_user, give, db):
    give("alice", "owner", "mesh", app.ids["mesh"])
    alice = db.get(m.User, app.ids["users"]["alice"])
    alice.identities = [m.UserIdentity(subject="u:alice-orcid", idp="ORCID")]
    team = m.Team(name="Testers")
    team.members.append(m.TeamMember(user=alice))
    db.add(team)
    db.commit()
    admin = as_user("admin")
    page = admin.get("/access/users/%d" % app.ids["users"]["alice"]).get_data(as_text=True)
    assert "Delete account" in page and "Nobody else can change this item" in page and "Demo mesh" in page
    assert admin.post("/access/users/%d" % app.ids["users"]["alice"], {"action": "delete"}).status_code == 302
    db.expire_all()
    assert db.get(m.User, app.ids["users"]["alice"]) is None
    assert db.query(m.Grant).filter_by(principal_type="user", principal_id=app.ids["users"]["alice"]).count() == 0
    assert db.get(m.UserIdentity, "u:alice-orcid") is None
    assert db.query(m.Team).filter_by(name="Testers").one().members == []
    entry = db.query(m.AuditLog).filter_by(action="delete-user").one()
    assert entry.object_label == "Alice" and entry.detail["left to administrators"] == ["Demo mesh"]
    assert db.get(m.Mesh, app.ids["mesh"]) is not None           # the mesh itself stays


def test_account_deletion_is_for_administrators_and_not_oneself(app, as_user, db):
    assert as_user("alice").post("/access/users/%d" % app.ids["users"]["bob"], {"action": "delete"}).status_code == 403
    as_user("admin").post("/access/users/%d" % app.ids["users"]["admin"], {"action": "delete"})
    db.expire_all()
    assert db.get(m.User, app.ids["users"]["bob"]) is not None
    assert db.get(m.User, app.ids["users"]["admin"]) is not None


def test_only_changer_ignores_items_someone_else_can_change(app, give, db):
    give("alice", "owner", "mesh", app.ids["mesh"])
    give("alice", "editor", "hostgroup", app.ids["g1"])
    give("bob", "owner", "hostgroup", app.ids["g1"])
    give("bob", "contributor", "hostgroup", app.ids["g2"])
    give("alice", "contributor", "hostgroup", app.ids["g2"])
    alice = db.get(m.User, app.ids["users"]["alice"])
    assert [obj.id for _, obj in authz.only_changer_of(db, alice)] == [app.ids["mesh"]]
    db.get(m.User, app.ids["users"]["bob"]).active = False       # a disabled account does not count
    db.commit()
    left = sorted((gr.scope_type, obj.id) for gr, obj in authz.only_changer_of(db, alice))
    assert left == sorted([("mesh", app.ids["mesh"]), ("hostgroup", app.ids["g1"]), ("hostgroup", app.ids["g2"])])


def test_federated_login_creates_accounts(tmp_path):
    """With PWA_AUTH=oidc the identity comes from Apache mod_auth_openidc."""
    app = create_app({"AUTH": "oidc", "DATABASE_URL": "sqlite:///%s" % (tmp_path / "o.db"),
                      "PUBLISH_DIR": str(tmp_path / "pub"), "SECRET_KEY": "k", "TESTING": True,
                      "ADMINS": ["http://cilogon.org/serverA/users/1"]})
    c = app.test_client()
    assert c.get("/").status_code == 401                               # nobody signed in
    assert c.get("/pub/config").status_code in (200, 404)              # published files stay open
    env = {"REMOTE_USER": "http://cilogon.org/serverA/users/1", "OIDC_CLAIM_name": "Ada Admin",
           "OIDC_CLAIM_email": "ada@example.edu", "OIDC_CLAIM_idp_name": "Example University"}
    assert c.get("/access", environ_overrides=env).status_code == 200
    visitor = {"REMOTE_USER": "http://cilogon.org/serverT/users/77", "OIDC_CLAIM_name": "Vera Visitor",
               "OIDC_CLAIM_idp_name": "Universität Beispiel (eduGAIN)"}
    assert c.get("/meshes", environ_overrides=visitor).status_code == 200
    assert c.get("/meshes/new", environ_overrides=visitor).status_code == 403
    s = app.extensions["pwa_session"]()
    people = {u.name: u for u in s.query(m.User)}
    assert people["Ada Admin"].is_admin and people["Ada Admin"].idp == "Example University"
    assert not people["Vera Visitor"].is_admin and people["Vera Visitor"].idp.endswith("(eduGAIN)")


def test_one_person_can_have_several_logins(tmp_path):
    app = create_app({"AUTH": "oidc", "DATABASE_URL": "sqlite:///%s" % (tmp_path / "o.db"),
                      "PUBLISH_DIR": str(tmp_path / "pub"), "SECRET_KEY": "k", "TESTING": True})
    s = app.extensions["pwa_session"]()
    ada = m.User(subject="http://cilogon.org/serverA/users/1", name="Ada", is_admin=True)
    ada.identities = [m.UserIdentity(subject="http://cilogon.org/serverE/users/9", idp="ORCID")]
    s.add(ada)
    s.commit()
    c = app.test_client()
    assert c.get("/access", environ_overrides={"REMOTE_USER": "http://cilogon.org/serverE/users/9"}).status_code == 200
    assert s.query(m.User).count() == 1                      # recognised, no second account created


def test_imported_access_is_offered_by_email_but_needs_confirming(app, as_user, give, db):
    from pwa import authz
    old = m.User(subject="pwa:17", name="B. Builder (imported)", email="Bob@Example.org")
    db.add(old)
    db.commit()
    authz.grant(db, old, "owner", "mesh", db.get(m.Mesh, app.ids["mesh"]))
    bob = db.get(m.User, app.ids["users"]["bob"])
    bob.email = "bob@example.org"
    db.commit()
    assert [(u.name, p.name) for u, p in authz.link_candidates(db)] == [("Bob", "B. Builder (imported)")]
    rename = {"name": "Bob's now", "slug": "demo"}
    assert as_user("bob").post("/meshes/%d" % app.ids["mesh"], rename).status_code == 403   # not automatic
    assert b"previous PWA uses your e-mail" in as_user("bob").get("/").data
    assert b"Confirm: same person" in as_user("admin").get("/").data
    assert as_user("bob").post("/access/users/%d" % old.id, {"action": "merge", "target_id": bob.id}).status_code == 403
    as_user("admin").post("/access/users/%d" % old.id, {"action": "merge", "target_id": bob.id})
    assert as_user("bob").post("/meshes/%d" % app.ids["mesh"], rename).status_code == 302
    assert authz.link_candidates(db) == []


def test_merging_two_real_logins_keeps_both_working(app, db):
    from pwa import authz
    a, b = db.get(m.User, app.ids["users"]["alice"]), db.get(m.User, app.ids["users"]["bob"])
    authz.merge_users(db, a, b)
    db.commit()
    assert db.get(m.UserIdentity, "u:alice").user_id == b.id
