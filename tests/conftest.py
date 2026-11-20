import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pwa import authz, models as m          # noqa: E402
from pwa.web import create_app, publish_now  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    app = create_app({"AUTH": "dev", "DATABASE_URL": "sqlite:///%s" % (tmp_path / "t.db"),
                      "PUBLISH_DIR": str(tmp_path / "pub"), "PUBLIC_URL": "https://ps.example.org/pub",
                      "SECRET_KEY": "test", "TESTING": True})
    Session = app.extensions["pwa_session"]
    s = Session()
    hosts = {n: m.Host(hostname=n + ".example.org", site="Site " + n.upper(), source="lookup",
                       capabilities=caps, communities=comm)
             for n, caps, comm in (("a", ["throughput", "rtt"], ["WLCG"]), ("b", ["throughput"], ["WLCG"]),
                                   ("c", ["latency", "latencybg"], []), ("d", ["throughput"], ["WLCG", "OSG"]),
                                   ("e", [], []))}
    g1 = m.HostGroup(name="Throughput hosts", intended_test="throughput", members=[hosts["a"], hosts["b"]])
    g2 = m.HostGroup(name="Latency hosts", intended_test="latencybg", members=[hosts["c"]])
    test = m.Test(name="Six-hour throughput", type="throughput", spec={"duration": "PT25S", "ip-version": 4},
                  tools=["iperf3"], repeat="PT6H", slip="PT3H")
    archive = m.Archive(name="Central", definition={"archiver": "http", "data": {
        "_url": "https://archive.example.org/logstash", "op": "put"}})
    mesh = m.Mesh(slug="demo", name="Demo mesh", archives=[archive])
    mesh.tasks = [m.MeshTask(position=0, name="Demo throughput", topology="mesh", test=test, group_a=g1)]
    people = {"admin": m.User(subject="u:admin", name="Ada Admin", is_admin=True),
              "alice": m.User(subject="u:alice", name="Alice"),
              "bob": m.User(subject="u:bob", name="Bob")}
    s.add_all(list(hosts.values()) + [g1, g2, test, archive, mesh] + list(people.values()))
    s.commit()
    publish_now(app, s)
    app.ids = {"hosts": {k: v.id for k, v in hosts.items()}, "g1": g1.id, "g2": g2.id, "test": test.id,
               "archive": archive.id, "mesh": mesh.id, "task": mesh.tasks[0].id,
               "users": {k: v.id for k, v in people.items()}}
    Session.remove()
    yield app


@pytest.fixture()
def db(app):
    Session = app.extensions["pwa_session"]
    yield Session()
    Session.remove()


class Browser:
    """A signed-in test client that sends the CSRF token like the pages do."""

    def __init__(self, app, user_id):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["uid"], sess["csrf"] = user_id, "tok"

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, data=None, htmx=False, **kw):
        data = dict(data or {}, _csrf="tok")
        headers = {"HX-Request": "true"} if htmx else {}
        return self.client.post(url, data=data, headers=headers, **kw)


@pytest.fixture()
def as_user(app):
    return lambda name: Browser(app, app.ids["users"][name])


@pytest.fixture()
def give(app):
    """give("alice", "editor", "mesh", mesh_id_or_None)"""
    def _give(name, role, scope_type, scope_id=None):
        Session = app.extensions["pwa_session"]
        s = Session()
        obj = s.get(m.SCOPE_MODELS[scope_type], scope_id) if scope_id else None
        authz.grant(s, s.get(m.User, app.ids["users"][name]), role, scope_type, obj)
        s.commit()
        Session.remove()
    return _give
