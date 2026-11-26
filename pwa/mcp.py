"""MCP (Model Context Protocol) server: pwa-next for AI assistants.

    pwa mcp --as <login> [--allow-changes]

Speaks JSON-RPC 2.0 over standard input and output, one message per line, as
the MCP stdio transport specifies.  Written with the standard library only,
because the MCP SDK needs a newer Python than EL9 ships.

The server acts as one pwa-next account, named by its login (the CILogon
subject, a password-file name, or an e-mail address).  That account's
permissions decide what it may change, exactly as on the web pages; every
change is recorded under History as "<name> (via MCP)" and published at once.
Without --allow-changes only the read tools are offered.
"""
import datetime
import json
import sys
import traceback

from . import __version__, authz, db, models as m, publish, reports, services, specforms, testinfo

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")   # newest first
MAX_LIST = 200

INSTRUCTIONS = (
    "pwa-next holds the measurement plan for a set of perfSONAR hosts: hosts, host groups, tests, "
    "archives and meshes. A mesh is made of tasks; a task runs one test between the hosts of one group "
    "(every pair) or between two groups. Hosts read their configuration from published pSConfig files. "
    "Look things up before changing them; refer to hosts by full host name, to meshes by short name "
    "(slug), and to groups and tests by name. Changes are published immediately and recorded in History.")


class ToolError(Exception):
    """A problem the caller can fix; reported as a tool result with isError."""


# --------------------------------------------------------------------------
# lookups shared by the tools
# --------------------------------------------------------------------------

def _iso(dt):
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _find(session, model, key, *, fields, kind):
    """One object by id or by one of `fields` (case-insensitive); helpful error otherwise."""
    if key is None or str(key).strip() == "":
        raise ToolError("Say which %s." % kind)
    text = str(key).strip()
    if text.isdigit():
        obj = session.get(model, int(text))
        if obj is not None:
            return obj
    for field in fields:
        col = getattr(model, field)
        found = session.query(model).filter(col.ilike(text.replace("%", r"\%").replace("_", r"\_"),
                                                      escape="\\")).all()
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ToolError("More than one %s is called %r; use its id: %s" % (
                kind, text, ", ".join(str(o.id) for o in found)))
    like = "%" + text.lower().replace("%", r"\%").replace("_", r"\_") + "%"
    near = session.query(model).filter(getattr(model, fields[0]).ilike(like, escape="\\")).limit(8).all()
    hint = ("; did you mean: " + ", ".join(repr(getattr(o, fields[0])) for o in near)) if near else ""
    raise ToolError("No %s %r%s." % (kind, text, hint))


def _host(session, name):
    return _find(session, m.Host, name, fields=("hostname", "display_name"), kind="host")


def _group(session, name):
    return _find(session, m.HostGroup, name, fields=("name",), kind="host group")


def _test(session, name):
    return _find(session, m.Test, name, fields=("name",), kind="test")


def _mesh(session, name):
    return _find(session, m.Mesh, name, fields=("slug", "name"), kind="mesh")


def _stale_cutoff():
    return datetime.datetime.utcnow() - datetime.timedelta(days=reports.STALE_DAYS)


def _host_row(h):
    info = h.info or {}
    return {"hostname": h.hostname, "name": h.shown_name if hasattr(h, "shown_name") else h.hostname,
            "site": h.site, "source": h.source, "communities": h.communities or [],
            "offers": h.capabilities or [], "version": info.get("pshost-toolkitversion")
            or info.get("pshost-bundle-version"), "last_seen_in_lookup_service": _iso(h.last_seen),
            "gone_quiet": bool(h.source == "lookup" and h.last_seen and h.last_seen < _stale_cutoff())}


def _task_row(t, membership=None):
    row = {"task": t.name, "mesh": t.mesh.slug, "enabled": t.enabled, "test": t.test.name,
           "test_type": t.test.type, "repeat": t.test.repeat or "continuous", "topology": t.topology,
           "group": t.group_a.name if t.group_a else None}
    if t.topology == "disjoint":
        row["second_group"] = t.group_b.name if t.group_b else None
    if membership is not None:
        hosts = {h.id for h in membership.of(t.group_a)}
        if t.topology == "disjoint":
            hosts |= {h.id for h in membership.of(t.group_b)}
        row["hosts"] = len(hosts)
    return row


def _holders(session, scope_type, obj):
    out = []
    for g, p in authz.grants_on(session, scope_type, obj):
        out.append({"who": p.name if isinstance(p, m.Team) else p.label,
                    "kind": "team" if isinstance(p, m.Team) else "person", "role": g.role,
                    "for": "this item" if g.scope_id else "every item of this kind"})
    return out


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------

class Server:
    def __init__(self, session_factory, cfg, login, allow_changes=False):
        self.Session, self.cfg, self.allow_changes = session_factory, cfg, allow_changes
        s = self.Session()
        try:
            self.user_id = self._resolve_user(s, login).id
        finally:
            s.close()
        self.tools = [t for t in TOOLS if allow_changes or not t["changes"]]
        self.initialized = False

    @staticmethod
    def _resolve_user(s, login):
        login = (login or "").strip()
        if not login:
            raise SystemExit("pwa mcp: say which account to act as (--as <login>)")
        user = s.query(m.User).filter_by(subject=login).one_or_none()
        if user is None:
            alias = s.get(m.UserIdentity, login)
            user = alias.user if alias else None
        if user is None:
            found = s.query(m.User).filter(m.User.email.ilike(login)).all()
            real = [u for u in found if not u.is_placeholder]
            if len(real) > 1:
                raise SystemExit("pwa mcp: more than one account uses %s; give its login instead" % login)
            user = real[0] if real else None
        if user is None:
            raise SystemExit("pwa mcp: no account %r (it appears after its first sign-in on the web)" % login)
        if user.is_placeholder:
            raise SystemExit("pwa mcp: %r is an imported account not yet tied to a login" % login)
        if not user.active:
            raise SystemExit("pwa mcp: the account %s is disabled" % user.label)
        return user

    # -- JSON-RPC -------------------------------------------------------------

    def handle(self, msg):
        """One incoming message -> the response object, or None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
        method, params, mid = msg["method"], msg.get("params") or {}, msg.get("id")
        notification = "id" not in msg
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [_describe(t) for t in self.tools]}
            elif method == "tools/call":
                result = self._call(params)
            elif method.startswith("notifications/"):
                if method == "notifications/initialized":
                    self.initialized = True
                return None
            else:
                return None if notification else _error(mid, -32601, "Method not found: %s" % method)
        except _RpcError as e:
            return None if notification else _error(mid, e.code, e.message)
        except Exception:                       # never let one call end the session
            traceback.print_exc(file=sys.stderr)
            return None if notification else _error(mid, -32603, "Internal error")
        return None if notification else {"jsonrpc": "2.0", "id": mid, "result": result}

    def _initialize(self, params):
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        s = self.Session()
        try:
            user = s.get(m.User, self.user_id)
            who = "%s%s" % (user.label, ", service administrator" if user.is_admin else "")
        finally:
            s.close()
        mode = ("Changes are allowed, within this account's permissions."
                if self.allow_changes else "Read-only: no change tools are offered.")
        return {"protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pwa-next", "title": self.cfg.get("SITE_NAME") or "pwa-next",
                               "version": __version__},
                "instructions": "%s Acting as %s. %s" % (INSTRUCTIONS, who, mode)}

    def _call(self, params):
        name, args = params.get("name"), params.get("arguments") or {}
        tool = next((t for t in self.tools if t["name"] == name), None)
        if tool is None:
            raise _RpcError(-32602, "Unknown tool: %s" % name)
        if not isinstance(args, dict):
            raise _RpcError(-32602, "arguments must be an object")
        s = self.Session()
        try:
            user = s.get(m.User, self.user_id)
            if user is None or not user.active:
                return _result({"error": "This account has been removed or disabled."}, error=True)
            ctx = Context(s, self.cfg, user)
            try:
                out = tool["fn"](ctx, **args)
            except TypeError as e:
                s.rollback()
                return _result({"error": "Bad arguments for %s: %s" % (name, e)}, error=True)
            except ToolError as e:
                s.rollback()
                return _result({"error": str(e)}, error=True)
            return _result(out)
        finally:
            s.close()


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _result(data, error=False):
    return {"content": [{"type": "text", "text": json.dumps(data, indent=1, ensure_ascii=False, default=str)}],
            "structuredContent": data, "isError": error}


def _describe(t):
    d = {"name": t["name"], "title": t["title"], "description": t["description"],
         "inputSchema": {"type": "object", "properties": t.get("params", {}),
                         "required": t.get("required", []), "additionalProperties": False}}
    d["annotations"] = {"title": t["title"], "readOnlyHint": not t["changes"],
                        "destructiveHint": bool(t.get("destructive")), "idempotentHint": not t["changes"]
                        or bool(t.get("idempotent")), "openWorldHint": False}
    return d


class Context:
    """What a tool gets: the session, settings, the acting account and its permissions."""

    def __init__(self, session, cfg, user):
        self.s, self.cfg, self.user = session, cfg, user
        self.access = authz.Access(session, user)
        self.actor = "%s (via MCP)" % user.label

    def need(self, action, scope_type, obj=None):
        if not self.access.can(action, scope_type, obj):
            what = getattr(obj, "label", None) or getattr(obj, "name", None) or scope_type
            raise ToolError("%s may not %s %s." % (self.user.label, {
                "edit": "change", "members": "change the hosts of", "delete": "delete"}.get(action, action), what))

    def commit_and_publish(self):
        self.s.commit()
        r = publish.publish_all(self.s, self.cfg["PUBLISH_DIR"], self.cfg["PUBLIC_URL"])
        return {"files_written": r["written"],
                "not_published": [{"file": p, "reason": why} for p, why in r["failed"]][:10],
                "withheld_tasks": [{"mesh": slug, "task": t, "reason": why} for slug, t, why in r["withheld"]][:10]}


# --------------------------------------------------------------------------
# read tools
# --------------------------------------------------------------------------

def t_overview(ctx):
    s = ctx.s
    all_tasks = reports.tasks(s)
    withheld = []
    for mesh in s.query(m.Mesh).order_by(m.Mesh.slug):
        _, held = publish.mesh_status(s, mesh)
        withheld += [{"mesh": mesh.slug, "task": t.name, "reasons": errs} for t, errs in held]
    stale = reports.stale(all_tasks)
    last = s.query(m.Publication).order_by(m.Publication.published.desc()).first() \
        if hasattr(m, "Publication") else None
    return {"counts": {"meshes": s.query(m.Mesh).count(), "host_groups": s.query(m.HostGroup).count(),
                       "tests": s.query(m.Test).count(), "hosts": s.query(m.Host).count(),
                       "enabled_tasks": len(all_tasks)},
            "withheld_tasks": withheld,
            "hosts_gone_quiet_by_mesh": [{"mesh": x["mesh"].slug, "hosts": len(x["hosts"])} for x in stale],
            "esmond_archives_in_use": [a.name for a in s.query(m.Archive) if a.archiver == "esmond"
                                       and services.archive_usage(s, a).get("meshes")],
            "last_published": _iso(getattr(last, "published", None)),
            "acting_as": ctx.user.label, "service_administrator": ctx.user.is_admin}


def t_find_hosts(ctx, text, test_type=None, limit=25):
    limit = max(1, min(int(limit), MAX_LIST))
    return {"hosts": [_host_row(h) for h in services.suggest_hosts(ctx.s, text, test_type, limit=limit)]}


def t_host_info(ctx, host):
    h = _host(ctx.s, host)
    usage = services.host_usage(ctx.s, h)
    row = _host_row(h)
    row.update({"addresses": [a.get("address") for a in h.addresses or []],
                "in_groups": [g.name for g in usage["direct"]],
                "in_groups_by_rule_or_inclusion": [g.name for g in usage["effective"] if g not in usage["direct"]],
                "excluded_from": [g.name for g in usage["excluded_from"]],
                "tasks": [_task_row(t) for t in usage["tasks"]],
                "meshes": [x.slug for x in usage["meshes"]],
                "can_change": ctx.access.can("edit", "host", h)})
    return row


def t_list_meshes(ctx):
    summary = {x["mesh"].id: x for x in reports.mesh_summary(reports.tasks(ctx.s))}
    rows = []
    for mesh in ctx.s.query(m.Mesh).order_by(m.Mesh.slug):
        x = summary.get(mesh.id)
        rows.append({"mesh": mesh.slug, "name": mesh.name, "tasks": len(mesh.tasks),
                     "enabled_tasks": sum(1 for t in mesh.tasks if t.enabled),
                     "hosts": x["hosts"] if x else 0, "measured_pairs": x["measurements"] if x else 0})
    return {"meshes": rows}


def t_mesh_info(ctx, mesh):
    mesh = _mesh(ctx.s, mesh)
    mem = services.Membership(ctx.s)
    problems, held = publish.mesh_status(ctx.s, mesh)
    return {"mesh": mesh.slug, "name": mesh.name, "description": getattr(mesh, "description", None),
            "published_at": "%s/config/%s" % (ctx.cfg["PUBLIC_URL"].rstrip("/"), mesh.slug),
            "archives": [a.name for a in mesh.archives],
            "tasks": [_task_row(t, mem) for t in mesh.tasks],
            "withheld_tasks": [{"task": t.name, "reasons": e} for t, e in held],
            "problems": problems, "who_can_change": _holders(ctx.s, "mesh", mesh),
            "can_change": ctx.access.can("edit", "mesh", mesh)}


def t_list_groups(ctx, text=None):
    q = ctx.s.query(m.HostGroup).order_by(m.HostGroup.name)
    if text:
        q = q.filter(m.HostGroup.name.ilike("%" + text.replace("%", r"\%") + "%", escape="\\"))
    mem = services.Membership(ctx.s)
    rows = [{"group": g.name, "id": g.id, "hosts": len(mem.of(g)), "rule": bool(g.rule),
             "used_by_tasks": len(services.tasks_using_group(ctx.s, g))} for g in q.limit(MAX_LIST)]
    return {"groups": rows}


def t_group_info(ctx, group):
    g = _group(ctx.s, group)
    mem = services.Membership(ctx.s)
    direct = {h.id for h in g.members}
    return {"group": g.name, "id": g.id, "description": g.description, "intended_test": g.intended_test,
            "rule": g.rule, "includes": [c.name for c in g.includes],
            "hosts": [{"hostname": h.hostname, "how": "listed" if h.id in direct else "rule or included group"}
                      for h in mem.of(g)],
            "excluded": [h.hostname for h in g.excludes],
            "used_by_tasks": [_task_row(t) for t in services.tasks_using_group(ctx.s, g)],
            "who_can_change": _holders(ctx.s, "hostgroup", g),
            "can_change_hosts": ctx.access.can("members", "hostgroup", g)}


def t_list_tests(ctx, test_type=None):
    q = ctx.s.query(m.Test).order_by(m.Test.name)
    if test_type:
        q = q.filter(m.Test.type == test_type)
    return {"tests": [{"test": t.name, "id": t.id, "type": t.type, "repeat": t.repeat or "continuous",
                       "used_by_tasks": len(services.test_usage(ctx.s, t))} for t in q.limit(MAX_LIST)]}


def t_test_info(ctx, test):
    t = _test(ctx.s, test)
    errors = publish.Builder(ctx.s).spec_errors(t)
    return {"test": t.name, "id": t.id, "type": t.type, "description": t.description,
            "options": t.spec or {}, "tools": t.tools or [], "repeat": t.repeat or "continuous",
            "start_window": t.slip, "problems": errors,
            "used_by_tasks": [_task_row(x) for x in services.test_usage(ctx.s, t)],
            "can_change": ctx.access.can("edit", "test", t)}


def t_test_type_help(ctx, test_type):
    known = specforms.available("tests")
    if test_type not in known:
        raise ToolError("Unknown test type %r; known: %s" % (test_type, ", ".join(sorted(known))))
    return {"type": test_type, "title": testinfo.title(test_type), "measures": testinfo.measured(test_type),
            "guidance": testinfo.guidance(test_type), "addressing": testinfo.addressing(test_type),
            "tools": testinfo.tools(test_type),
            "options": [f["name"] for f in specforms.fields("tests", test_type)]
            if specforms.known("tests", test_type) else []}


REPORTS = ("gone_quiet", "follow_up_by_site", "duplicate_measurements", "busiest_hosts", "capability_gaps",
           "versions", "meshes")


def t_report(ctx, name, limit=25):
    limit = max(1, min(int(limit), MAX_LIST))
    all_tasks = reports.tasks(ctx.s)
    if name == "gone_quiet":
        return {"report": name, "rows": [{"mesh": x["mesh"].slug, "hosts": [
            {"hostname": h.hostname, "last_seen": _iso(h.last_seen)} for h in x["hosts"]][:limit]}
            for x in reports.stale(all_tasks)]}
    if name == "follow_up_by_site":
        from . import sites
        reg = sites.registry(ctx.s)
        return {"report": name, "registry_read": reg.get("fetched"), "sites": [
            {"site": g["site"], "country": g["country"], "tier": g["tier"], "federation": g["federation"],
             "ngi": g["ngi"], "experiments": g["experiments"], "how_to_reach": g["reach"], "gocdb_page": g["gocdb"],
             "hosts": [{"hostname": x["host"].hostname, "last_release": x["release"], "silent_days": x["silent_days"],
                        "ask": x["action_text"], "meshes": x["meshes"], "cric_entry": x["cric"]} for x in g["hosts"]]}
            for g in reports.follow_up(all_tasks, reg)[:limit]]}
    if name == "duplicate_measurements":
        return {"report": name, "rows": [{"type": o["type"], "pairs": o["pairs"], "same_settings": o["same_settings"],
                                          "tasks": ["%s / %s" % (x.mesh.slug, x.task.name) for x, _ in o["tasks"]]}
                                         for o in reports.overlaps(all_tasks, limit=limit)]}
    if name == "busiest_hosts":
        rows = sorted(reports.host_load(all_tasks), key=lambda e: -e["throughput_share"])[:limit]
        return {"report": name, "rows": [{"hostname": e["host"].hostname, "meshes": e["meshes"],
                                          "partners": e["partners"], "throughput_runs_per_day": round(e["throughput_runs"]),
                                          "share_of_day_in_throughput_tests": round(e["throughput_share"], 3),
                                          "continuous_streams": e["streams"]} for e in rows]}
    if name == "capability_gaps":
        return {"report": name, "rows": [{"mesh": g["task"].mesh.slug, "task": g["task"].task.name,
                                          "test_type": g["task"].type,
                                          "hosts": [h.hostname for h in g["hosts"]][:limit]}
                                         for g in reports.capability_gaps(all_tasks)[:limit]]}
    if name == "versions":
        v = reports.versions(all_tasks)
        return {"report": name, "hosts": v["total"], "by_release": [{"release": r, "hosts": n} for r, n in v["rows"]],
                "older_than_5": [h.hostname for h in v["old"]][:limit]}
    if name == "meshes":
        return {"report": name, "rows": [{"mesh": x["mesh"].slug, "tasks": x["tasks"], "hosts": x["hosts"],
                                          "measured_pairs": x["measurements"], "runs_per_day": round(x["runs_per_day"]),
                                          "throughput_hours_per_day": round(x["throughput_hours"], 1)}
                                         for x in reports.mesh_summary(all_tasks)]}
    raise ToolError("Unknown report %r; choose one of: %s" % (name, ", ".join(REPORTS)))


def t_tested_between(ctx, host, other_host=None):
    a = _host(ctx.s, host)
    b = _host(ctx.s, other_host) if other_host else None
    rows = reports.between(reports.tasks(ctx.s), a, b)
    return {"host": a.hostname, "other_host": b.hostname if b else None,
            "tasks": [dict(_task_row(r["task"].task), direction=r["direction"]) for r in rows]}


SUMMARY_OVER = 20000      # bytes of JSON; a busy host's feed runs to 150 kB


def t_published_config(ctx, mesh=None, host=None, full=False):
    if bool(mesh) == bool(host):
        raise ToolError("Give either a mesh or a host.")
    b = publish.Builder(ctx.s)
    if mesh:
        x = _mesh(ctx.s, mesh)
        good, _ = b.publishable([t for t in x.tasks if t.enabled])
        out, template = {"mesh": x.slug}, b.mesh_template(x, good)
    else:
        h = _host(ctx.s, host)
        good, _ = b.publishable(b.tasks_by_host().get(h.id, []))
        out, template = {"host": h.hostname}, b.host_template(h, good)
    size = len(json.dumps(template))
    if full or size <= SUMMARY_OVER:
        out["template"] = template
    else:
        out.update({"size_bytes": size, "tasks": sorted(template.get("tasks", {})),
                    "archives": sorted(template.get("archives", {})), "groups": len(template.get("groups", {})),
                    "note": "Summary only: the JSON is %d bytes. Call again with full set to true for all of it." % size})
    return out


def t_history(ctx, limit=20, text=None):
    limit = max(1, min(int(limit), MAX_LIST))
    q = ctx.s.query(m.AuditLog).order_by(m.AuditLog.id.desc())
    if text:
        like = "%" + text.replace("%", r"\%") + "%"
        q = q.filter(m.AuditLog.object_label.ilike(like, escape="\\") | m.AuditLog.actor.ilike(like, escape="\\"))
    return {"entries": [{"at": _iso(e.at), "who": e.actor, "action": e.action, "what": e.object_label,
                         "kind": e.object_type, "detail": e.detail} for e in q.limit(limit)]}


# --------------------------------------------------------------------------
# change tools (offered only with --allow-changes)
# --------------------------------------------------------------------------

def _hosts_named(ctx, names):
    if not isinstance(names, list) or not names:
        raise ToolError("Give a list of host names.")
    found, unknown = [], []
    for n in dict.fromkeys(str(x).strip().lower() for x in names if str(x).strip()):
        h = ctx.s.query(m.Host).filter(m.Host.hostname.ilike(n)).one_or_none()
        (found if h else unknown).append(h or n)
    return found, unknown


def t_add_hosts_to_group(ctx, group, hosts):
    g = _group(ctx.s, group)
    ctx.need("members", "hostgroup", g)
    found, unknown = _hosts_named(ctx, hosts)
    added = []
    for h in found:
        if h in g.excludes:
            g.excludes.remove(h)
        if h not in g.members:
            g.members.append(h)
            added.append(h.hostname)
    out = {"group": g.name, "added": added, "already_members": [h.hostname for h in found if h.hostname not in added],
           "not_in_inventory": unknown}
    if added:
        services.audit(ctx.s, ctx.actor, "add-host", g, {"hosts": added})
        out["published"] = ctx.commit_and_publish()
    return out


def t_remove_hosts_from_group(ctx, group, hosts, exclude=False):
    g = _group(ctx.s, group)
    ctx.need("members", "hostgroup", g)
    found, unknown = _hosts_named(ctx, hosts)
    mem = {h.id for h in services.Membership(ctx.s).of(g)}
    removed, excluded, not_members = [], [], []
    for h in found:
        if h in g.members:
            g.members.remove(h)
            removed.append(h.hostname)
        elif h.id in mem and exclude:
            g.excludes.append(h)
            excluded.append(h.hostname)
        elif h.id in mem:
            not_members.append(h.hostname + " (comes from a rule or an included group: set exclude to true)")
        else:
            not_members.append(h.hostname)
    out = {"group": g.name, "removed": removed, "excluded": excluded, "not_removed": not_members,
           "not_in_inventory": unknown}
    if removed:
        services.audit(ctx.s, ctx.actor, "remove-host", g, {"hosts": removed})
    if excluded:
        services.audit(ctx.s, ctx.actor, "exclude-host", g, {"hosts": excluded})
    if removed or excluded:
        out["published"] = ctx.commit_and_publish()
    return out


def t_remove_host_everywhere(ctx, host, confirm=False):
    h = _host(ctx.s, host)
    ctx.need("delete", "host", h)
    usage = services.host_usage(ctx.s, h)
    preview = {"host": h.hostname, "listed_in_groups": [g.name for g in usage["direct"]],
               "in_groups_by_rule": [g.name for g in usage["by_rule"]],
               "meshes": [x.slug for x in usage["meshes"]], "tasks": len(usage["tasks"])}
    if not confirm:
        preview["note"] = ("Nothing changed. Call again with confirm set to true to take the host out of "
                           "these groups and delete it.")
        return preview
    summary = services.remove_host(ctx.s, h, ctx.actor)
    return {"removed": summary, "published": ctx.commit_and_publish()}


def t_set_task_enabled(ctx, mesh, task, enabled):
    x = _mesh(ctx.s, mesh)
    ctx.need("edit", "mesh", x)
    found = [t for t in x.tasks if t.name.lower() == str(task).strip().lower()]
    if len(found) != 1:
        raise ToolError("Mesh %s has no task %r; its tasks: %s" % (x.slug, task, ", ".join(t.name for t in x.tasks)))
    t = found[0]
    if t.enabled == bool(enabled):
        return {"mesh": x.slug, "task": t.name, "enabled": t.enabled, "changed": False}
    t.enabled = bool(enabled)
    services.audit(ctx.s, ctx.actor, "enable-task" if t.enabled else "disable-task", x, {"task": t.name})
    return {"mesh": x.slug, "task": t.name, "enabled": t.enabled, "changed": True,
            "published": ctx.commit_and_publish()}


def t_update_test(ctx, test, options=None, repeat=None):
    t = _test(ctx.s, test)
    ctx.need("edit", "test", t)
    if options is None and repeat is None:
        raise ToolError("Nothing to change: give options and/or repeat.")
    before = {"spec": dict(t.spec or {}), "repeat": t.repeat}
    spec = dict(t.spec or {})
    if options is not None:
        if not isinstance(options, dict):
            raise ToolError("options must be an object of option names and values (null removes one).")
        for k, v in options.items():
            if k in specforms.PARTICIPANT_FIELDS:
                raise ToolError("%r is set per task from the host groups, not in the test." % k)
            if v is None:
                spec.pop(k, None)
            else:
                spec[k] = v
    if repeat is not None:
        if str(repeat).strip().lower() == "continuous":
            t.repeat = t.slip = None
        else:
            try:
                t.repeat = specforms.normalize_duration(str(repeat))
            except ValueError:
                raise ToolError("repeat needs a duration such as 6h, 30m or PT6H, or \"continuous\".")
    t.spec = spec
    errors = specforms.validate("tests", t.type, spec) + testinfo.problems(t.type, spec, t.tools or [])
    if errors:
        raise ToolError("Not saved: " + "; ".join(errors))
    after = {"spec": t.spec, "repeat": t.repeat}
    services.audit(ctx.s, ctx.actor, "edit", t, {k: [before[k], after[k]] for k in before if before[k] != after[k]})
    return {"test": t.name, "options": t.spec, "repeat": t.repeat or "continuous",
            "tasks_affected": len(services.test_usage(ctx.s, t)), "published": ctx.commit_and_publish()}


# --------------------------------------------------------------------------
# the tool table
# --------------------------------------------------------------------------

S = {"type": "string"}
TOOLS = [
    {"name": "overview", "title": "Overview", "changes": False, "fn": t_overview,
     "description": "Counts, tasks withheld from publication and why, hosts that have stopped registering "
                    "(per mesh), and esmond archives still in use. Start here."},
    {"name": "find_hosts", "title": "Find hosts", "changes": False, "fn": t_find_hosts,
     "description": "Hosts whose name, site or display name contains every word of the text. "
                    "With test_type, hosts that offer that test type come first.",
     "params": {"text": S, "test_type": S, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST}},
     "required": ["text"]},
    {"name": "host_info", "title": "Host details", "changes": False, "fn": t_host_info,
     "description": "One host: what the Lookup Service says about it (site, communities, test types it "
                    "offers, version, last seen), its groups, and every mesh task it takes part in.",
     "params": {"host": S}, "required": ["host"]},
    {"name": "list_meshes", "title": "List meshes", "changes": False, "fn": t_list_meshes,
     "description": "Every mesh with its task count and how many hosts and host pairs it measures."},
    {"name": "mesh_info", "title": "Mesh details", "changes": False, "fn": t_mesh_info,
     "description": "One mesh (by short name or name): its tasks, archives, withheld tasks with reasons, "
                    "and who can change it.", "params": {"mesh": S}, "required": ["mesh"]},
    {"name": "list_groups", "title": "List host groups", "changes": False, "fn": t_list_groups,
     "description": "Host groups, optionally only those whose name contains text, with sizes and use.",
     "params": {"text": S}},
    {"name": "group_info", "title": "Host group details", "changes": False, "fn": t_group_info,
     "description": "One host group: its rule, included groups, every effective host and how it got there, "
                    "exclusions, the tasks that use it, and who can change it.",
     "params": {"group": S}, "required": ["group"]},
    {"name": "list_tests", "title": "List tests", "changes": False, "fn": t_list_tests,
     "description": "Tests, optionally of one test type (throughput, latencybg, trace, rtt, ...).",
     "params": {"test_type": S}},
    {"name": "test_info", "title": "Test details", "changes": False, "fn": t_test_info,
     "description": "One test: its options, tools, repeat interval, any problems that would make "
                    "pScheduler reject it, and the tasks that use it.", "params": {"test": S}, "required": ["test"]},
    {"name": "test_type_help", "title": "What a test type does", "changes": False, "fn": t_test_type_help,
     "description": "What a pScheduler test type measures, when to use it, which tools run it, and its options.",
     "params": {"test_type": S}, "required": ["test_type"]},
    {"name": "report", "title": "Reports", "changes": False, "fn": t_report,
     "description": "One of the Reports page's analyses: gone_quiet (hosts that stopped registering, by mesh), "
                    "follow_up_by_site (the same hosts by the site that runs them, with what to ask and how to "
                    "reach the site), "
                    "duplicate_measurements (host pairs measured by several tasks), busiest_hosts, "
                    "capability_gaps (hosts in tasks they may not support), versions, meshes (load per mesh).",
     "params": {"name": {"type": "string", "enum": list(REPORTS)},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST}}, "required": ["name"]},
    {"name": "tested_between", "title": "What is tested between hosts", "changes": False, "fn": t_tested_between,
     "description": "The tasks that measure between two hosts, or every task one host takes part in.",
     "params": {"host": S, "other_host": S}, "required": ["host"]},
    {"name": "published_config", "title": "Published pSConfig", "changes": False, "fn": t_published_config,
     "description": "The pSConfig JSON published for a mesh, or the per-host feed a perfSONAR host reads. "
                    "Large ones come back as a summary unless full is true.",
     "params": {"mesh": S, "host": S, "full": {"type": "boolean"}}},
    {"name": "history", "title": "History", "changes": False, "fn": t_history,
     "description": "Recent changes, newest first: who did what to which item. text filters by item or person.",
     "params": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST}, "text": S}},
    {"name": "add_hosts_to_group", "title": "Add hosts to a group", "changes": True, "idempotent": True,
     "fn": t_add_hosts_to_group,
     "description": "Add hosts (full host names) to a host group; every mesh that uses the group includes them "
                    "from the next publication. Hosts must already be in the inventory.",
     "params": {"group": S, "hosts": {"type": "array", "items": S, "minItems": 1}}, "required": ["group", "hosts"]},
    {"name": "remove_hosts_from_group", "title": "Remove hosts from a group", "changes": True, "idempotent": True,
     "fn": t_remove_hosts_from_group,
     "description": "Take hosts out of a host group. A host that comes from the group's rule or an included "
                    "group can only be excluded: set exclude to true for that.",
     "params": {"group": S, "hosts": {"type": "array", "items": S, "minItems": 1}, "exclude": {"type": "boolean"}},
     "required": ["group", "hosts"]},
    {"name": "remove_host_everywhere", "title": "Remove a host from everything", "changes": True,
     "destructive": True, "fn": t_remove_host_everywhere,
     "description": "Take a host out of every group and delete it. Without confirm it only lists what would "
                    "change; call again with confirm true to do it.",
     "params": {"host": S, "confirm": {"type": "boolean"}}, "required": ["host"]},
    {"name": "set_task_enabled", "title": "Enable or disable a task", "changes": True, "idempotent": True,
     "fn": t_set_task_enabled,
     "description": "Enable or disable one task of a mesh. A disabled task stays in the mesh but is not published.",
     "params": {"mesh": S, "task": S, "enabled": {"type": "boolean"}}, "required": ["mesh", "task", "enabled"]},
    {"name": "update_test", "title": "Change a test", "changes": True, "idempotent": True, "fn": t_update_test,
     "description": "Change a test's options (an object of option names and values; null removes an option) "
                    "and/or how often it repeats (6h, PT30M, or \"continuous\"). Checked against pScheduler's "
                    "schema before saving; every task using the test changes.",
     "params": {"test": S, "options": {"type": "object"}, "repeat": S}, "required": ["test"]},
]


# --------------------------------------------------------------------------
# stdio transport
# --------------------------------------------------------------------------

def serve(server, stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "Parse error")
        else:
            reply = _error(None, -32600, "Batches are not supported") if isinstance(msg, list) else server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
            stdout.flush()


def main(cfg, login, allow_changes=False):
    engine = db.make_engine(cfg["DATABASE_URL"])
    server = Server(db.make_session_factory(engine), cfg, login, allow_changes)
    print("pwa-next MCP server %s, acting as %s%s" % (
        __version__, login, ", changes allowed" if allow_changes else ", read-only"), file=sys.stderr)
    serve(server)
    return 0
