"""Merging identical tasks (PWA_MERGE_DUPLICATES): each ordered pair measured once."""
import collections

from pwa import models as m, publish


def scheduled_pairs(template, host):
    """What the pSConfig agent on `host` runs: ordered pairs with `host` as source.

    Follows pSConfig 5.2.6: mesh = every ordered pair of its addresses, disjoint =
    a x b and b x a, an exclude (local-address -> target-addresses) removes one
    direction only, and a pair is scheduled by its source.
    """
    out = collections.Counter()
    for key, task in template["tasks"].items():
        g = template["groups"][task["group"]]
        if g["type"] == "mesh":
            names = [a["name"] for a in g["addresses"]]
            pairs = [(x, y) for x in names for y in names if x != y]
        elif g["type"] == "disjoint":
            a = [x["name"] for x in g["a-addresses"]]
            b = [x["name"] for x in g["b-addresses"]]
            pairs = [(x, y) for x in a for y in b if x != y] + [(y, x) for x in a for y in b if x != y]
        else:
            continue
        excluded = {(e["local-address"]["name"], t["name"]) for e in g.get("excludes", [])
                    for t in e["target-addresses"]}
        for p in pairs:
            if p[0] == host and p not in excluded:
                out[(template["tests"][task["test"]]["type"], p)] += 1
    return out


def _two_meshes_tracing_the_same_pairs(db):
    hosts = {n: db.query(m.Host).filter_by(hostname=n + ".example.org").one() for n in "abcde"}
    trace = dict(type="trace", spec={"ip-version": 6}, repeat="PT10M")
    t1, t2 = m.Test(name="Trace one", **trace), m.Test(name="Trace two", **trace)
    g1 = m.HostGroup(name="Experiment X", members=[hosts["a"], hosts["b"], hosts["c"]])
    g2 = m.HostGroup(name="Experiment Y", members=[hosts["b"], hosts["c"], hosts["d"]])
    archive = db.query(m.Archive).one()
    x = m.Mesh(slug="x", name="X mesh", archives=[archive])
    y = m.Mesh(slug="y", name="Y mesh", archives=[archive])
    x.tasks = [m.MeshTask(position=0, name="X trace", topology="mesh", test=t1, group_a=g1)]
    y.tasks = [m.MeshTask(position=0, name="Y trace", topology="mesh", test=t2, group_a=g2)]
    db.add_all([t1, t2, g1, g2, x, y])
    db.commit()
    return hosts


def feeds(db, merge):
    b = publish.Builder(db, merge=merge)
    by_host = b.tasks_by_host()
    out = {}
    for h in db.query(m.Host):
        good, _ = b.publishable(by_host.get(h.id, []))
        out[h.hostname] = b.host_template(h, good)
    return b, out


def test_each_pair_is_traced_once_and_credited_to_both_meshes(app, db):
    _two_meshes_tracing_the_same_pairs(db)
    _, plain = feeds(db, merge=())
    before = sum((scheduled_pairs(t, h) for h, t in plain.items()), collections.Counter())
    traces = {p: n for (kind, p), n in before.items() if kind == "trace"}
    assert traces[("b.example.org", "c.example.org")] == 2              # both meshes trace b -> c
    assert not any("excludes" in g for t in plain.values() for g in t["groups"].values())

    builder, merged = feeds(db, merge=("trace",))
    after = sum((scheduled_pairs(t, h) for h, t in merged.items()), collections.Counter())
    assert {p for (kind, p) in after if kind == "trace"} == set(traces)     # nothing lost
    assert all(n == 1 for (kind, _), n in after.items() if kind == "trace")  # nothing twice
    assert sum(after.values()) == sum(before.values()) - 2                  # b->c and c->b saved
    feed = merged["b.example.org"]
    shared = [t for k, t in feed["tasks"].items() if "[shared" in k]
    assert len(shared) == 1 and shared[0]["reference"]["display-task-group"] == ["X mesh", "Y mesh"]
    assert feed["groups"][shared[0]["group"]]["b-addresses"] == [{"name": "c.example.org"}]
    for template in merged.values():
        assert builder.problems(template) == []                            # valid pSConfig
    assert "excludes" not in builder.mesh_template(db.query(m.Mesh).filter_by(slug="x").one())["groups"]["X trace"]


def test_only_identical_tasks_merge(app, db):
    _two_meshes_tracing_the_same_pairs(db)
    db.query(m.Test).filter_by(name="Trace two").one().repeat = "PT20M"     # no longer identical
    db.commit()
    _, merged = feeds(db, merge=("trace",))
    assert not any("[shared" in k for t in merged.values() for k in t["tasks"])
    _, other_type = feeds(db, merge=("throughput",))
    assert not any("[shared" in k for t in other_type.values() for k in t["tasks"])


def test_published_files_follow_the_setting(app, db, tmp_path):
    _two_meshes_tracing_the_same_pairs(db)
    import json
    publish.publish_all(db, str(tmp_path / "on"), "https://ps.example.org/pub", merge=("trace",))
    with open(tmp_path / "on" / "auto" / "b.example.org", encoding="utf-8") as f:
        assert any("[shared" in k for k in json.load(f)["tasks"])
