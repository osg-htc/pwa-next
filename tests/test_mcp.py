import io
import json
import os
import subprocess
import sys

import pytest

from pwa import mcp, models as m


def server(app, login="u:admin", changes=False):
    return mcp.Server(app.extensions["pwa_session"], dict(app.config), login, allow_changes=changes)


def call(srv, _tool, **args):
    r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": _tool, "arguments": args}})
    assert r["id"] == 7 and "result" in r, r
    res = r["result"]
    assert json.loads(res["content"][0]["text"]) == json.loads(json.dumps(res["structuredContent"], default=str))
    return res


def test_handshake_and_read_only_tool_list(app):
    srv = server(app)
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["capabilities"]["tools"] == {"listChanged": False}
    assert "Ada Admin" in init["instructions"] and "Read-only" in init["instructions"]
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    # an unknown protocol version gets the newest one we speak
    assert srv.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                       "params": {"protocolVersion": "1999-01-01"}})["result"]["protocolVersion"] == "2025-06-18"
    names = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]]
    assert "mesh_info" in names and "add_hosts_to_group" not in names
    tools = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    with_changes = server(app, changes=True)
    tools = {t["name"]: t for t in with_changes.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})["result"]["tools"]}
    assert tools["remove_host_everywhere"]["annotations"]["destructiveHint"] is True
    assert tools["add_hosts_to_group"]["annotations"]["readOnlyHint"] is False


def test_read_tools(app):
    srv = server(app, "u:alice")                        # anyone signed in may look
    o = call(srv, "overview")["structuredContent"]
    assert o["counts"]["meshes"] == 1 and o["acting_as"] == "Alice"
    found = call(srv, "find_hosts", text="example", test_type="latencybg")["structuredContent"]["hosts"]
    assert found[0]["hostname"] == "c.example.org"
    h = call(srv, "host_info", host="A.example.org")["structuredContent"]
    assert h["in_groups"] == ["Throughput hosts"] and h["meshes"] == ["demo"] and h["can_change"] is False
    mesh = call(srv, "mesh_info", mesh="demo")["structuredContent"]
    assert mesh["tasks"][0]["hosts"] == 2 and mesh["archives"] == ["Central"]
    assert call(srv, "mesh_info", mesh="Demo mesh")["structuredContent"]["mesh"] == "demo"
    g = call(srv, "group_info", group="throughput hosts")["structuredContent"]
    assert [x["hostname"] for x in g["hosts"]] == ["a.example.org", "b.example.org"]
    t = call(srv, "test_info", test="Six-hour throughput")["structuredContent"]
    assert t["repeat"] == "PT6H" and t["used_by_tasks"][0]["mesh"] == "demo"
    assert call(srv, "test_type_help", test_type="throughput")["structuredContent"]["title"]
    for name in mcp.REPORTS:
        assert call(srv, "report", name=name)["isError"] is False
    b = call(srv, "tested_between", host="a.example.org", other_host="b.example.org")["structuredContent"]
    assert b["tasks"][0]["direction"] == "both directions"
    pub = call(srv, "published_config", mesh="demo")["structuredContent"]["template"]
    assert "Demo throughput" in pub["tasks"]
    feed = call(srv, "published_config", host="b.example.org")["structuredContent"]["template"]
    assert "Demo throughput" in feed["tasks"]
    assert isinstance(call(srv, "history")["structuredContent"]["entries"], list)
    mcp.SUMMARY_OVER, before = 10, mcp.SUMMARY_OVER
    try:
        short = call(srv, "published_config", mesh="demo")["structuredContent"]
        assert "template" not in short and short["tasks"] == ["Demo throughput"] and "full" in short["note"]
        assert "template" in call(srv, "published_config", mesh="demo", full=True)["structuredContent"]
    finally:
        mcp.SUMMARY_OVER = before


def test_lookups_explain_what_is_missing(app):
    srv = server(app)
    r = call(srv, "host_info", host="a.example")
    assert r["isError"] and "did you mean" in r["structuredContent"]["error"]
    r = call(srv, "report", name="nonsense")
    assert r["isError"] and "gone_quiet" in r["structuredContent"]["error"]
    assert call(srv, "published_config")["isError"]
    assert call(srv, "mesh_info")["isError"]                     # missing argument


def test_changes_need_permission_and_are_recorded(app, give, db):
    alice = server(app, "u:alice", changes=True)
    r = call(alice, "add_hosts_to_group", group="Throughput hosts", hosts=["d.example.org"])
    assert r["isError"] and "may not" in r["structuredContent"]["error"]
    give("alice", "contributor", "hostgroup", app.ids["g1"])
    r = call(alice, "add_hosts_to_group", group="Throughput hosts", hosts=["D.example.org", "nope.example.org"])
    out = r["structuredContent"]
    assert out["added"] == ["d.example.org"] and out["not_in_inventory"] == ["nope.example.org"]
    db.expire_all()
    assert "d.example.org" in [h.hostname for h in db.get(m.HostGroup, app.ids["g1"]).members]
    entry = db.query(m.AuditLog).filter_by(action="add-host").one()
    assert entry.actor == "Alice (via MCP)"
    with open(os.path.join(app.config["PUBLISH_DIR"], "config", "demo"), encoding="utf-8") as f:
        assert "d.example.org" in f.read()
    # a contributor may change members, not the mesh
    assert call(alice, "set_task_enabled", mesh="demo", task="Demo throughput", enabled=False)["isError"]
    r = call(alice, "remove_hosts_from_group", group="Throughput hosts", hosts=["a.example.org", "c.example.org"])
    assert r["structuredContent"]["removed"] == ["a.example.org"]
    assert r["structuredContent"]["not_removed"] == ["c.example.org"]


def test_remove_host_everywhere_previews_first(app, db):
    srv = server(app, changes=True)
    r = call(srv, "remove_host_everywhere", host="a.example.org")["structuredContent"]
    assert r["listed_in_groups"] == ["Throughput hosts"] and "Nothing changed" in r["note"]
    db.expire_all()
    assert db.query(m.Host).filter_by(hostname="a.example.org").count() == 1
    r = call(srv, "remove_host_everywhere", host="a.example.org", confirm=True)["structuredContent"]
    assert r["removed"]["groups"] == ["Throughput hosts"]
    db.expire_all()
    assert db.query(m.Host).filter_by(hostname="a.example.org").count() == 0
    assert db.query(m.AuditLog).filter_by(action="remove-host").one().actor == "Ada Admin (via MCP)"


def test_task_and_test_changes(app, db):
    srv = server(app, changes=True)
    r = call(srv, "set_task_enabled", mesh="demo", task="demo throughput", enabled=False)["structuredContent"]
    assert r["changed"] is True and r["enabled"] is False
    assert call(srv, "set_task_enabled", mesh="demo", task="Demo throughput", enabled=False)["structuredContent"]["changed"] is False
    assert call(srv, "set_task_enabled", mesh="demo", task="Nope", enabled=True)["isError"]
    r = call(srv, "update_test", test="Six-hour throughput", options={"duration": "PT30S"}, repeat="4h")
    assert r["isError"] is False and r["structuredContent"]["repeat"] == "PT4H"
    db.expire_all()
    t = db.get(m.Test, app.ids["test"])
    assert t.spec["duration"] == "PT30S" and t.repeat == "PT4H"
    bad = call(srv, "update_test", test="Six-hour throughput", options={"duration": "a while"})
    assert bad["isError"] and "Not saved" in bad["structuredContent"]["error"]
    assert call(srv, "update_test", test="Six-hour throughput", options={"dest": "x.example.org"})["isError"]
    db.expire_all()
    assert db.get(m.Test, app.ids["test"]).spec["duration"] == "PT30S"     # the bad change was not kept
    without = server(app, changes=False)
    r = without.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                        "params": {"name": "update_test", "arguments": {"test": "x"}}})
    assert r["error"]["code"] == -32602                                    # not offered when read-only


def test_who_the_server_acts_as(app, db):
    db.add_all([m.User(subject="pwa:9", name="Imported", email="imp@example.org"),
                m.User(subject="u:off", name="Off", active=False),
                m.User(subject="u:carol", name="Carol", email="Carol@Example.org")])
    db.commit()
    for login in ("u:nobody", "pwa:9", "u:off", ""):
        with pytest.raises(SystemExit):
            server(app, login)
    assert "Carol" in server(app, "carol@example.org").handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]["instructions"]


def test_stdio_transport(app):
    srv = server(app)
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
        json.dumps([{"jsonrpc": "2.0", "id": 2, "method": "ping"}]),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "resources/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}}),
        ""]) + "\n"
    out = io.StringIO()
    mcp.serve(srv, io.StringIO(lines), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, None, 3, 4, 5]
    assert replies[0]["result"]["protocolVersion"] == "2025-03-26"
    assert replies[1]["error"]["code"] == -32700 and replies[2]["error"]["code"] == -32600
    assert replies[3]["result"] == {} and replies[4]["error"]["code"] == -32601
    assert replies[5]["error"]["code"] == -32602


def test_command_line_over_pipes(app, tmp_path):
    env_file = tmp_path / "pwa.env"
    env_file.write_text("PWA_DATABASE_URL=%s\nPWA_PUBLISH_DIR=%s\nPWA_PUBLIC_URL=https://ps.example.org/pub\n"
                        "PWA_SECRET_KEY=test\n" % (app.config["DATABASE_URL"], app.config["PUBLISH_DIR"]))
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_meshes", "arguments": {}}}]
    root = os.path.join(os.path.dirname(__file__), "..")
    p = subprocess.run([sys.executable, "-m", "pwa", "mcp", "--as", "u:admin"],
                       input="".join(json.dumps(x) + "\n" for x in msgs), capture_output=True, text=True,
                       timeout=60, cwd=root, env=dict(os.environ, PWA_CONFIG=str(env_file), PYTHONPATH=root))
    assert p.returncode == 0, p.stderr
    replies = [json.loads(x) for x in p.stdout.splitlines()]       # stdout holds nothing but messages
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["structuredContent"]["meshes"][0]["mesh"] == "demo"
    assert "read-only" in p.stderr
