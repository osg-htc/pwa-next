"""Pages.  Each handler checks permission, changes the database through
pwa.services, records an audit entry and republishes."""
import datetime
import json
import os
import re

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session as cookie, url_for)
from sqlalchemy import func, or_

from .. import authz, services, specforms
from .. import models as m
from .. import publish as publisher
from . import publish_now

ui = Blueprint("ui", __name__)
pub = Blueprint("pub", __name__, url_prefix="/pub")
dev = Blueprint("dev", __name__, url_prefix="/dev")
blueprints = [ui, pub, dev]

STALE_DAYS = 14
COMMON_INTERVALS = ["PT5M", "PT10M", "PT30M", "PT1H", "PT2H", "PT4H", "PT6H", "PT12H", "P1D"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def need(action, scope_type, obj=None):
    if not g.access.can(action, scope_type, obj):
        abort(403, "You do not have permission to do that. Ask an owner of this "
                   "item, or a service administrator, for access.")


def find(model, ident):
    obj = g.db.get(model, ident)
    if obj is None:
        abort(404)
    return obj


def done(message, action=None, obj=None, detail=None, republish=True, object_type=None, label=None):
    """Record, commit, republish, tell the user."""
    if action:
        services.audit(g.db, g.user, action, obj, detail, object_type=object_type, label=label)
    g.db.commit()
    if republish:
        result = publish_now(current_app, g.db)
        for path, reason in result["failed"][:3]:
            flash("Not published: %s — %s" % (path, reason), "error")
        for slug, task, reason in result["withheld"][:3]:
            flash("Task “%s” in %s is withheld from publication: %s" % (task, slug, reason), "warn")
    flash(message, "ok")


def back(default):
    """Return to the page the form was on, but never to another site."""
    ref = request.referrer or ""
    return redirect(ref if ref.startswith(request.host_url) else url_for(default))


def is_htmx():
    return request.headers.get("HX-Request") == "true"


def own(obj):
    """Whoever creates something owns it."""
    g.db.flush()
    if not g.user.is_admin:
        authz.grant(g.db, g.user, "owner", obj.object_type, obj, by=g.user)


def unique_name(model, name, ignore_id=None):
    q = g.db.query(model).filter(func.lower(model.name) == name.lower())
    if ignore_id:
        q = q.filter(model.id != ignore_id)
    return q.first() is None


def stale_cutoff():
    return datetime.datetime.utcnow() - datetime.timedelta(days=STALE_DAYS)


def access_context(scope_type, obj):
    return {"grants": authz.grants_on(g.db, scope_type, obj),
            "can_grant": g.access.can("grant", scope_type, obj),
            "grant_roles": authz.roles_for(scope_type, False), "role_help": authz.ROLE_HELP,
            "scope_type": scope_type}


# --------------------------------------------------------------------------
# dashboard, audit
# --------------------------------------------------------------------------

@ui.route("/")
def dashboard():
    db = g.db
    counts = {name: db.query(model).count() for name, model in
              (("hosts", m.Host), ("groups", m.HostGroup), ("tests", m.Test),
               ("archives", m.Archive), ("meshes", m.Mesh))}
    member_ids = {row[0] for row in db.query(m.group_members.c.host_id).distinct()}
    counts["hosts_in_use"] = len(member_ids)
    stale = (db.query(m.Host).filter(m.Host.source == "lookup", m.Host.last_seen < stale_cutoff(),
                                     m.Host.id.in_(member_ids)).order_by(m.Host.last_seen).all()
             if member_ids else [])
    problems = db.query(m.Publication).filter(or_(m.Publication.ok.is_(False),
                                                  m.Publication.message != "")).all()
    mesh_problems = []
    for p in problems:
        if p.path.startswith("config/"):
            mesh = db.query(m.Mesh).filter_by(slug=p.path[len("config/"):]).one_or_none()
            withheld = publisher.mesh_status(db, mesh)[1] if mesh else []
            mesh_problems.append((p, mesh, withheld))
    esmond = [a for a in db.query(m.Archive) if a.archiver == "esmond"]
    esmond_use = [(a, services.archive_usage(db, a)) for a in esmond]
    used_tests = {row[0] for row in db.query(m.MeshTask.test_id).distinct()}
    used_groups = {row[0] for row in db.query(m.MeshTask.group_a_id).distinct()} | \
                  {row[0] for row in db.query(m.MeshTask.group_b_id).distinct()} | \
                  {row[0] for row in db.query(m.group_includes.c.child_id).distinct()}
    unused = {"tests": counts["tests"] - len(used_tests),
              "groups": db.query(m.HostGroup).filter(~m.HostGroup.id.in_(used_groups or {0})).count()}
    placeholders = db.query(m.User).filter(m.User.subject.like("pwa:%")).count()
    links = authz.link_candidates(db)
    my_link = next((p for u, p in links if u.id == g.user.id), None)
    recent = db.query(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(12).all()
    last = db.query(func.max(m.Publication.published)).scalar()
    return render_template("dashboard.html", counts=counts, stale=stale, mesh_problems=mesh_problems,
                           host_problem_count=len(problems) - len(mesh_problems),
                           esmond_use=[(a, u) for a, u in esmond_use if u["meshes"] or u["hosts"]],
                           unused=unused, placeholders=placeholders, recent=recent,
                           links=links if g.user.is_admin else [], my_link=my_link,
                           last_published=last, stale_days=STALE_DAYS)


@ui.route("/audit")
def audit_log():
    q = (request.args.get("q") or "").strip()
    query = g.db.query(m.AuditLog)
    if q:
        like = "%" + q + "%"
        query = query.filter(or_(m.AuditLog.actor.ilike(like), m.AuditLog.object_label.ilike(like),
                                 m.AuditLog.action.ilike(like)))
    return render_template("audit.html", entries=query.order_by(m.AuditLog.id.desc()).limit(300).all(), q=q)


@ui.route("/publish", methods=["POST"])
def publish_all():
    result = publish_now(current_app, g.db)
    flash("Published: %d file(s) updated, %d unchanged, %d held back." % (
        result["written"], result["unchanged"], len(result["failed"])), "ok")
    return back("ui.dashboard")


# --------------------------------------------------------------------------
# suggestions (type-ahead for every picker)
# --------------------------------------------------------------------------

@ui.route("/suggest/<kind>")
def suggest(kind):
    text = (request.args.get("_q") or "").strip()
    words = text.lower().split()
    items = []
    if kind == "host":
        for h in services.suggest_hosts(g.db, text, request.args.get("test") or None):
            items.append((h.id, h.hostname, h.shown_name))
    elif kind in ("hostgroup", "test", "archive", "team"):
        model = {"hostgroup": m.HostGroup, "test": m.Test, "archive": m.Archive, "team": m.Team}[kind]
        q = g.db.query(model)
        for w in words:
            q = q.filter(model.name.ilike("%" + w + "%"))
        membership = services.Membership(g.db)
        for o in q.order_by(model.name).limit(30):
            if kind == "hostgroup":
                hint = "%d hosts" % len(membership.of(o))
            elif kind == "test":
                hint = specforms.FRIENDLY_TYPES.get(o.type, o.type) + \
                    (", every " + current_app.jinja_env.filters["duration"](o.repeat) if o.repeat else "")
            elif kind == "archive":
                hint = o.archiver or ""
            else:
                hint = "%d members" % len(o.members)
            items.append((o.id, o.name, hint))
    elif kind in ("user", "principal"):
        prefix = "user:" if kind == "principal" else ""
        if kind == "principal":
            tq = g.db.query(m.Team)
            for w in words:
                tq = tq.filter(m.Team.name.ilike("%" + w + "%"))
            for t in tq.order_by(m.Team.name).limit(15):
                items.append(("team:%d" % t.id, t.name, "team, %d members" % len(t.members)))
        q = g.db.query(m.User)
        for w in words:
            like = "%" + w + "%"
            q = q.filter(or_(m.User.name.ilike(like), m.User.email.ilike(like), m.User.subject.ilike(like)))
        for u in q.filter(m.User.active.is_(True)).order_by(m.User.name).limit(30):
            items.append(("%s%d" % (prefix, u.id), u.label, u.email or u.idp or ""))
    else:
        abort(404)
    return render_template("_suggest.html", items=items, text=text)


# --------------------------------------------------------------------------
# hosts
# --------------------------------------------------------------------------

def _host_query():
    q = (request.args.get("q") or "").strip()
    show = request.args.get("show") or "used"
    query = g.db.query(m.Host)
    for w in q.lower().split():
        like = "%" + w + "%"
        query = query.filter(or_(m.Host.hostname.ilike(like), m.Host.display_name.ilike(like),
                                 m.Host.site.ilike(like)))
    members = g.db.query(m.group_members.c.host_id).distinct()
    if show == "used":
        query = query.filter(m.Host.id.in_(members))
    elif show == "unused":
        query = query.filter(~m.Host.id.in_(members))
    elif show == "stale":
        query = query.filter(m.Host.source == "lookup", m.Host.last_seen < stale_cutoff(),
                             m.Host.id.in_(members))
    elif show == "manual":
        query = query.filter(m.Host.source == "manual")
    total = query.count()
    hosts = query.order_by(m.Host.hostname).limit(200).all()
    counts = dict(g.db.query(m.group_members.c.host_id, func.count())
                  .filter(m.group_members.c.host_id.in_([h.id for h in hosts] or [0]))
                  .group_by(m.group_members.c.host_id).all())
    return {"hosts": hosts, "total": total, "q": q, "show": show, "group_counts": counts,
            "stale_before": stale_cutoff()}


@ui.route("/hosts")
def hosts():
    ctx = _host_query()
    return render_template("_host_rows.html" if is_htmx() else "hosts.html", **ctx)


@ui.route("/hosts/new", methods=["GET", "POST"])
def host_new():
    need("create", "host")
    values = {"hostname": "", "display_name": ""}
    if request.method == "POST":
        values = {k: (request.form.get(k) or "").strip() for k in values}
        name = values["hostname"].lower()
        if not re.match(r"^[a-z0-9]([a-z0-9.:-]*[a-z0-9])?$", name):
            flash("Enter a host name or IP address.", "error")
        elif g.db.query(m.Host).filter_by(hostname=name).first():
            flash("%s is already in the inventory." % name, "error")
        else:
            host = m.Host(hostname=name, display_name=values["display_name"] or None, source="manual")
            g.db.add(host)
            own(host)
            done("Added %s." % name, "create", host)
            return redirect(url_for("ui.host", ident=host.id))
    return render_template("host_new.html", values=values)


@ui.route("/hosts/<int:ident>", methods=["GET", "POST"])
def host(ident):
    host = find(m.Host, ident)
    if request.method == "POST":
        need("edit", "host", host)
        before = {"display_name": host.display_name, "display_url": host.display_url,
                  "no_agent": host.no_agent, "archives": [a.name for a in host.archives]}
        host.display_name = (request.form.get("display_name") or "").strip() or None
        host.display_url = (request.form.get("display_url") or "").strip() or None
        host.no_agent = request.form.get("no_agent") == "on"
        ids = {int(x) for x in request.form.getlist("archives")}
        host.archives = [a for a in g.db.query(m.Archive) if a.id in ids]
        after = {"display_name": host.display_name, "display_url": host.display_url,
                 "no_agent": host.no_agent, "archives": [a.name for a in host.archives]}
        done("Saved %s." % host.hostname, "edit", host,
             {k: [before[k], after[k]] for k in before if before[k] != after[k]})
        return redirect(url_for("ui.host", ident=host.id))
    usage = services.host_usage(g.db, host)
    return render_template("host.html", host=host, usage=usage,
                           archives=g.db.query(m.Archive).order_by(m.Archive.name).all(),
                           can_edit=g.access.can("edit", "host", host),
                           can_delete=g.access.can("delete", "host", host),
                           stale=host.source == "lookup" and host.last_seen and host.last_seen < stale_cutoff(),
                           **access_context("host", host))


@ui.route("/hosts/<int:ident>/remove", methods=["GET", "POST"])
def host_remove(ident):
    host = find(m.Host, ident)
    need("delete", "host", host)
    usage = services.host_usage(g.db, host)
    if request.method == "POST":
        summary = services.remove_host(g.db, host, g.user)
        done("Removed %s from %d host group(s) and deleted it; %d mesh(es) republished." % (
            summary["hostname"], len(summary["groups"]), len(summary["meshes"])))
        return redirect(url_for("ui.hosts"))
    return render_template("host_remove.html", host=host, usage=usage)


# --------------------------------------------------------------------------
# host groups
# --------------------------------------------------------------------------

@ui.route("/groups")
def groups():
    q = (request.args.get("q") or "").strip()
    query = g.db.query(m.HostGroup)
    for w in q.lower().split():
        query = query.filter(m.HostGroup.name.ilike("%" + w + "%"))
    rows = query.order_by(m.HostGroup.name).all()
    membership = services.Membership(g.db)
    used = {}
    for t in g.db.query(m.MeshTask):
        for gid in (t.group_a_id, t.group_b_id):
            if gid:
                used.setdefault(gid, set()).add(t.mesh_id)
    ctx = {"groups": rows, "q": q, "sizes": {x.id: len(membership.of(x)) for x in rows}, "used": used,
           "friendly": specforms.FRIENDLY_TYPES}
    return render_template("_group_rows.html" if is_htmx() else "groups.html", **ctx)


def _rule_from_form(form):
    conditions = []
    for field, op, value in zip(form.getlist("rule_field"), form.getlist("rule_op"),
                                form.getlist("rule_value")):
        if field and value.strip():
            conditions.append({"field": field, "op": op or "is", "value": value.strip()})
    if not conditions:
        return None
    return {"match": "any" if form.get("rule_match") == "any" else "all", "conditions": conditions}


@ui.route("/groups/new", methods=["GET", "POST"])
def group_new():
    need("create", "hostgroup")
    values = {"name": "", "description": "", "intended_test": ""}
    if request.method == "POST":
        values = {k: (request.form.get(k) or "").strip() for k in values}
        if not values["name"]:
            flash("Give the group a name.", "error")
        elif not unique_name(m.HostGroup, values["name"]):
            flash("There is already a host group called “%s”." % values["name"], "error")
        else:
            group = m.HostGroup(name=values["name"], description=values["description"],
                                intended_test=values["intended_test"] or None)
            g.db.add(group)
            own(group)
            done("Created host group “%s”. Now add hosts." % group.name, "create", group, republish=False)
            return redirect(url_for("ui.group", ident=group.id))
    return render_template("group_new.html", values=values, test_types=specforms.available("tests"),
                           friendly=specforms.FRIENDLY_TYPES)


def _group_context(group):
    membership = services.Membership(g.db)
    members = membership.of(group)
    return {"group": group, "members": members, "why": membership.why(group),
            "direct_ids": {h.id for h in group.members},
            "can_members": g.access.can("members", "hostgroup", group),
            "can_edit": g.access.can("edit", "hostgroup", group),
            "stale_before": stale_cutoff()}


@ui.route("/groups/<int:ident>", methods=["GET", "POST"])
def group(ident):
    group = find(m.HostGroup, ident)
    if request.method == "POST":
        need("edit", "hostgroup", group)
        name = (request.form.get("name") or "").strip()
        rule = _rule_from_form(request.form)
        errors = services.validate_rule(rule)
        if not name:
            errors.append("the group needs a name")
        elif not unique_name(m.HostGroup, name, group.id):
            errors.append("another host group is already called “%s”" % name)
        if errors:
            flash("Not saved: " + "; ".join(errors) + ".", "error")
        else:
            before = len(services.Membership(g.db).of(group))
            changed = {}
            if group.rule != rule:
                changed["rule"] = [group.rule, rule]
            if group.name != name:
                changed["name"] = [group.name, name]
            group.name, group.rule = name, rule
            group.description = (request.form.get("description") or "").strip()
            group.intended_test = request.form.get("intended_test") or None
            g.db.flush()
            after = len(services.Membership(g.db).of(group))
            done("Saved “%s” (%d hosts%s)." % (group.name, after,
                 "" if before == after else ", was %d" % before), "edit", group, changed)
        return redirect(url_for("ui.group", ident=group.id))
    ctx = _group_context(group)
    return render_template(
        "group.html", tasks=services.tasks_using_group(g.db, group),
        containers=services.groups_containing_group(group),
        can_delete=g.access.can("delete", "hostgroup", group),
        test_types=specforms.available("tests"), friendly=specforms.FRIENDLY_TYPES,
        rule_fields=services.RULE_FIELDS, rule_ops=services.RULE_OPS,
        **ctx, **access_context("hostgroup", group))


@ui.route("/groups/<int:ident>/suggest")
def group_suggest(ident):
    group = find(m.HostGroup, ident)
    text = (request.args.get("_q") or "").strip()
    hosts = []
    if text:
        hosts = services.suggest_hosts(g.db, text, group.intended_test,
                                       exclude_ids={h.id for h in group.members})
    return render_template("_group_suggest.html", group=group, hosts=hosts, text=text,
                           stale_before=stale_cutoff())


def _members_response(group):
    if is_htmx():
        resp = current_app.make_response(render_template("_members.html", **_group_context(group)))
        resp.headers["HX-Trigger"] = "members-changed"
        return resp
    return redirect(url_for("ui.group", ident=group.id))


@ui.route("/groups/<int:ident>/members", methods=["POST"])
def group_add_member(ident):
    group = find(m.HostGroup, ident)
    need("members", "hostgroup", group)
    host = find(m.Host, request.form.get("host_id", type=int))
    if host not in group.members:
        group.members.append(host)
        if host in group.excludes:
            group.excludes.remove(host)
        done("Added %s to “%s”." % (host.hostname, group.name), "add-host", group, {"host": host.hostname})
    return _members_response(group)


@ui.route("/groups/<int:ident>/members/bulk", methods=["POST"])
def group_add_bulk(ident):
    group = find(m.HostGroup, ident)
    need("members", "hostgroup", group)
    names = [n.lower() for n in re.split(r"[\s,;]+", request.form.get("hostnames") or "") if n]
    added, unknown = [], []
    for name in dict.fromkeys(names):
        host = g.db.query(m.Host).filter_by(hostname=name).one_or_none()
        if host is None:
            unknown.append(name)
        elif host not in group.members:
            group.members.append(host)
            added.append(name)
    if added:
        done("Added %d host(s) to “%s”." % (len(added), group.name), "add-host", group, {"hosts": added})
    if unknown:
        flash("Not in the inventory (add them under Hosts first): " + ", ".join(unknown), "warn")
    if not added and not unknown:
        flash("Nothing to add: those hosts are already members.", "warn")
    return redirect(url_for("ui.group", ident=group.id))


@ui.route("/groups/<int:ident>/members/<int:host_id>/<action>", methods=["POST"])
def group_member_action(ident, host_id, action):
    group, host = find(m.HostGroup, ident), find(m.Host, host_id)
    need("members", "hostgroup", group)
    if action == "remove" and host in group.members:
        group.members.remove(host)
        done("Removed %s from “%s”." % (host.hostname, group.name), "remove-host", group, {"host": host.hostname})
    elif action == "exclude" and host not in group.excludes:
        group.excludes.append(host)
        done("%s is now excluded from “%s”." % (host.hostname, group.name), "exclude-host", group,
             {"host": host.hostname})
    elif action == "unexclude" and host in group.excludes:
        group.excludes.remove(host)
        done("%s is no longer excluded from “%s”." % (host.hostname, group.name), "unexclude-host", group,
             {"host": host.hostname})
    if request.form.get("back") == "host":
        return redirect(url_for("ui.host", ident=host.id))
    return _members_response(group)


@ui.route("/groups/<int:ident>/includes", methods=["POST"])
def group_include(ident):
    group = find(m.HostGroup, ident)
    need("edit", "hostgroup", group)
    child = find(m.HostGroup, request.form.get("child_id", type=int))
    if services.would_loop(group, child):
        flash("“%s” already contains “%s”, so including it would make a loop." % (child.name, group.name), "error")
    elif child not in group.includes:
        group.includes.append(child)
        done("“%s” now includes every host in “%s”." % (group.name, child.name), "include-group", group,
             {"group": child.name})
    return redirect(url_for("ui.group", ident=group.id))


@ui.route("/groups/<int:ident>/includes/<int:child_id>/remove", methods=["POST"])
def group_uninclude(ident, child_id):
    group, child = find(m.HostGroup, ident), find(m.HostGroup, child_id)
    need("edit", "hostgroup", group)
    if child in group.includes:
        group.includes.remove(child)
        done("“%s” no longer includes “%s”." % (group.name, child.name), "uninclude-group", group,
             {"group": child.name})
    return redirect(url_for("ui.group", ident=group.id))


# --------------------------------------------------------------------------
# delete / clone, shared by groups, tests, archives, meshes
# --------------------------------------------------------------------------

KINDS = {"groups": (m.HostGroup, "hostgroup", "ui.group", "ui.groups"),
         "tests": (m.Test, "test", "ui.test", "ui.tests"),
         "archives": (m.Archive, "archive", "ui.archive", "ui.archives"),
         "meshes": (m.Mesh, "mesh", "ui.mesh", "ui.meshes")}


@ui.route("/<any(groups,tests,archives,meshes):kind>/<int:ident>/delete", methods=["POST"])
def delete(kind, ident):
    model, scope, detail_view, list_view = KINDS[kind]
    obj = find(model, ident)
    need("delete", scope, obj)
    label = obj.label
    blockers = services.delete_object(g.db, obj, g.user)
    if blockers:
        flash("“%s” is still in use, so it was not deleted: %s." % (
            label, "; ".join(blockers[:6]) + (" …" if len(blockers) > 6 else "")), "error")
        return redirect(url_for(detail_view, ident=ident))
    done("Deleted “%s”." % label)
    return redirect(url_for(list_view))


@ui.route("/<any(groups,tests,archives,meshes):kind>/<int:ident>/clone", methods=["POST"])
def clone(kind, ident):
    model, scope, detail_view, _ = KINDS[kind]
    src = find(model, ident)
    need("create", scope)
    name, n = src.name + " (copy)", 2
    while not unique_name(model, name):
        name, n = "%s (copy %d)" % (src.name, n), n + 1
    if model is m.HostGroup:
        new = m.HostGroup(name=name, description=src.description, intended_test=src.intended_test,
                          rule=src.rule, members=list(src.members), excludes=list(src.excludes),
                          includes=list(src.includes))
    elif model is m.Test:
        new = m.Test(name=name, description=src.description, type=src.type, spec=dict(src.spec or {}),
                     tools=list(src.tools or []), repeat=src.repeat, slip=src.slip, sliprand=src.sliprand)
    elif model is m.Archive:
        new = m.Archive(name=name, description=src.description, definition=json.loads(json.dumps(src.definition)))
    else:
        slug, k = src.slug + "-copy", 2
        while g.db.query(m.Mesh).filter_by(slug=slug).first():
            slug, k = "%s-copy-%d" % (src.slug, k), k + 1
        new = m.Mesh(name=name, slug=slug, description=src.description, archives=list(src.archives))
        # A copy starts with its tasks switched off so it publishes nothing by surprise.
        new.tasks = [m.MeshTask(position=t.position, name=t.name, topology=t.topology, test=t.test,
                                group_a=t.group_a, group_b=t.group_b, enabled=False) for t in src.tasks]
    g.db.add(new)
    own(new)
    done("Copied “%s” to “%s”." % (src.name, new.name), "clone", new, {"from": src.name})
    return redirect(url_for(detail_view, ident=new.id))


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

@ui.route("/tests")
def tests():
    rows = g.db.query(m.Test).order_by(m.Test.name).all()
    used = {}
    for t in g.db.query(m.MeshTask):
        used.setdefault(t.test_id, set()).add(t.mesh_id)
    return render_template("tests.html", tests=rows, used=used, friendly=specforms.FRIENDLY_TYPES,
                           problems={t.id: specforms.validate("tests", t.type, t.spec or {}) for t in rows},
                           test_types=specforms.available("tests"))


def _test_form(test, form=None, errors=()):
    fields = specforms.fields("tests", test.type) if specforms.known("tests", test.type) else []
    names = {f["name"] for f in fields}
    spec = test.spec or {}
    extra = {k: v for k, v in spec.items() if k not in names}
    return render_template(
        "test.html", test=test, fields=fields, spec=spec, form=form, errors=errors,
        extra_json=json.dumps(extra, indent=2) if extra else "",
        tools=specforms.TOOLS.get(test.type, []), friendly=specforms.FRIENDLY_TYPES,
        intervals=COMMON_INTERVALS, usage=services.test_usage(g.db, test) if test.id else [],
        can_edit=(g.access.can("edit", "test", test) if test.id else g.access.can("create", "test")),
        can_delete=bool(test.id) and g.access.can("delete", "test", test),
        **(access_context("test", test) if test.id else {"grants": [], "can_grant": False}))


def _apply_test_form(test):
    """Read the form into `test`.  Returns a list of problems."""
    form, errors = request.form, []
    test.name = (form.get("name") or "").strip()
    test.description = (form.get("description") or "").strip()
    if not test.name:
        errors.append("Give the test a name.")
    elif not unique_name(m.Test, test.name, test.id):
        errors.append("Another test is already called “%s”." % test.name)
    spec = {}
    if specforms.known("tests", test.type):
        try:
            spec = specforms.parse_form("tests", test.type, form)
        except specforms.SpecError as e:
            errors += e.errors
    if (form.get("spec_extra") or "").strip():
        try:
            extra = json.loads(form["spec_extra"])
            if not isinstance(extra, dict):
                raise ValueError("must be a JSON object")
            spec.update(extra)
        except ValueError as e:
            errors.append("Additional options: not valid JSON (%s)." % e)
    for k in specforms.PARTICIPANT_FIELDS:
        spec.pop(k, None)
    test.spec = spec
    tools = [t for t in form.getlist("tools") if t]
    tools += [t for t in re.split(r"[\s,]+", form.get("tools_other") or "") if t and t not in tools]
    test.tools = tools
    test.repeat = test.slip = None
    if form.get("runs") != "continuous":
        try:
            test.repeat = specforms.normalize_duration(form.get("repeat") or "")
        except ValueError:
            errors.append("“Repeat every” needs a duration such as 6h, 30m or PT6H.")
        if (form.get("slip") or "").strip():
            try:
                test.slip = specforms.normalize_duration(form["slip"])
            except ValueError:
                errors.append("“Start window” needs a duration such as 3h or PT3H.")
        elif test.repeat:
            test.slip = specforms.seconds_to_iso(specforms.iso_to_seconds(test.repeat) / 2)
    test.sliprand = True
    if not errors:
        errors += specforms.validate("tests", test.type, spec)
    return errors


@ui.route("/tests/new", methods=["GET", "POST"])
def test_new():
    need("create", "test")
    ttype = request.values.get("type") or "throughput"
    test = m.Test(name="", type=ttype, spec={}, tools=[], repeat=None if ttype == "latencybg" else "PT6H")
    if request.method == "POST":
        errors = _apply_test_form(test)
        if errors:
            g.db.expunge_all()
            return _test_form(test, request.form, errors), 400
        g.db.add(test)
        own(test)
        done("Created test “%s”." % test.name, "create", test, republish=False)
        return redirect(url_for("ui.test", ident=test.id))
    return _test_form(test)


@ui.route("/tests/<int:ident>", methods=["GET", "POST"])
def test(ident):
    test = find(m.Test, ident)
    if request.method == "POST":
        need("edit", "test", test)
        before = {"spec": dict(test.spec or {}), "repeat": test.repeat, "slip": test.slip,
                  "tools": list(test.tools or []), "name": test.name}
        errors = _apply_test_form(test)
        if errors:
            page = _test_form(test, request.form, errors)
            g.db.rollback()
            return page, 400
        after = {"spec": test.spec, "repeat": test.repeat, "slip": test.slip, "tools": test.tools,
                 "name": test.name}
        n = len(services.test_usage(g.db, test))
        done("Saved “%s”%s." % (test.name, "; %d task(s) republished" % n if n else ""), "edit", test,
             {k: [before[k], after[k]] for k in before if before[k] != after[k]})
        return redirect(url_for("ui.test", ident=test.id))
    return _test_form(test)


# --------------------------------------------------------------------------
# archives
# --------------------------------------------------------------------------

@ui.route("/archives")
def archives():
    rows = g.db.query(m.Archive).order_by(m.Archive.name).all()
    return render_template("archives.html", archives=rows,
                           usage={a.id: services.archive_usage(g.db, a) for a in rows},
                           archiver_types=specforms.available("archivers"))


def _archive_form(archive, form=None, errors=()):
    kind = archive.archiver
    fields = specforms.fields("archivers", kind) if specforms.known("archivers", kind) else []
    names = {f["name"] for f in fields}
    data = (archive.definition or {}).get("data") or {}
    extra = {k: v for k, v in data.items() if k not in names}
    top = {k: v for k, v in (archive.definition or {}).items() if k not in ("archiver", "data")}
    return render_template(
        "archive.html", archive=archive, fields=fields, spec=data, form=form, errors=errors,
        extra_json=json.dumps(extra, indent=2) if extra else "",
        top_json=json.dumps(top, indent=2) if top else "",
        usage=services.archive_usage(g.db, archive) if archive.id else {"meshes": [], "hosts": []},
        can_edit=(g.access.can("edit", "archive", archive) if archive.id else g.access.can("create", "archive")),
        can_delete=bool(archive.id) and g.access.can("delete", "archive", archive),
        **(access_context("archive", archive) if archive.id else {"grants": [], "can_grant": False}))


def _apply_archive_form(archive):
    form, errors = request.form, []
    kind = archive.archiver
    archive.name = (form.get("name") or "").strip()
    archive.description = (form.get("description") or "").strip()
    if not archive.name:
        errors.append("Give the archive a name.")
    elif not unique_name(m.Archive, archive.name, archive.id):
        errors.append("Another archive is already called “%s”." % archive.name)
    data = {}
    if specforms.known("archivers", kind):
        try:
            data = specforms.parse_form("archivers", kind, form)
        except specforms.SpecError as e:
            errors += e.errors
    definition = {"archiver": kind, "data": data}
    for label, key, target in (("Additional settings", "spec_extra", data),
                               ("Archive-level settings", "top_extra", definition)):
        if (form.get(key) or "").strip():
            try:
                extra = json.loads(form[key])
                if not isinstance(extra, dict):
                    raise ValueError("must be a JSON object")
                target.update({k: v for k, v in extra.items() if k not in ("archiver", "data")})
            except ValueError as e:
                errors.append("%s: not valid JSON (%s)." % (label, e))
    archive.definition = definition
    if not errors:
        errors += specforms.validate("archivers", kind, data)
    return errors


@ui.route("/archives/new", methods=["GET", "POST"])
def archive_new():
    need("create", "archive")
    kind = request.values.get("type") or "http"
    archive = m.Archive(name="", definition={"archiver": kind, "data": {}})
    if request.method == "POST":
        errors = _apply_archive_form(archive)
        if errors:
            g.db.expunge_all()
            return _archive_form(archive, request.form, errors), 400
        g.db.add(archive)
        own(archive)
        done("Created archive “%s”." % archive.name, "create", archive, republish=False)
        return redirect(url_for("ui.archive", ident=archive.id))
    return _archive_form(archive)


@ui.route("/archives/<int:ident>", methods=["GET", "POST"])
def archive(ident):
    archive = find(m.Archive, ident)
    if request.method == "POST":
        need("edit", "archive", archive)
        before = json.loads(json.dumps(archive.definition))
        errors = _apply_archive_form(archive)
        if errors:
            page = _archive_form(archive, request.form, errors)
            g.db.rollback()
            return page, 400
        done("Saved “%s”." % archive.name, "edit", archive,
             {"definition": [before, archive.definition]} if before != archive.definition else None)
        return redirect(url_for("ui.archive", ident=archive.id))
    return _archive_form(archive)


# --------------------------------------------------------------------------
# meshes
# --------------------------------------------------------------------------

@ui.route("/meshes")
def meshes():
    rows = g.db.query(m.Mesh).order_by(m.Mesh.name).all()
    pubs = {p.path: p for p in g.db.query(m.Publication).filter(m.Publication.path.like("config/%"))}
    return render_template("meshes.html", meshes=rows, pubs=pubs)


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _apply_mesh_form(mesh):
    form, errors = request.form, []
    mesh.name = (form.get("name") or "").strip()
    mesh.description = (form.get("description") or "").strip()
    slug = (form.get("slug") or "").strip() or _slug(mesh.name)
    if not mesh.name:
        errors.append("Give the mesh a name.")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", slug or ""):
        errors.append("The address may only contain letters, digits, dots, dashes and underscores.")
    elif g.db.query(m.Mesh).filter(m.Mesh.slug == slug, m.Mesh.id != (mesh.id or 0)).first():
        errors.append("Another mesh is already published at “%s”." % slug)
    mesh.slug = slug
    ids = {int(x) for x in form.getlist("archives")}
    mesh.archives = [a for a in g.db.query(m.Archive) if a.id in ids]
    return errors


@ui.route("/meshes/new", methods=["GET", "POST"])
def mesh_new():
    need("create", "mesh")
    mesh = m.Mesh(name="", slug="", description="")
    if request.method == "POST":
        with g.db.no_autoflush:
            errors = _apply_mesh_form(mesh)
        if errors:
            for e in errors:
                flash(e, "error")
            g.db.expunge_all()
        else:
            g.db.add(mesh)
            own(mesh)
            done("Created mesh “%s”. Now add tasks." % mesh.name, "create", mesh)
            return redirect(url_for("ui.mesh", ident=mesh.id))
    return render_template("mesh_new.html", mesh=mesh, form=request.form,
                           archives=g.db.query(m.Archive).order_by(m.Archive.name).all())


def _pairs(task, membership):
    a = len(membership.of(task.group_a))
    if task.topology == "disjoint":
        return a, len(membership.of(task.group_b)), 2 * a * len(membership.of(task.group_b))
    return a, None, a * (a - 1)


@ui.route("/meshes/<int:ident>", methods=["GET", "POST"])
def mesh(ident):
    mesh = find(m.Mesh, ident)
    if request.method == "POST":
        need("edit", "mesh", mesh)
        old_slug = mesh.slug
        with g.db.no_autoflush:
            errors = _apply_mesh_form(mesh)
        if errors:
            for e in errors:
                flash("Not saved: " + e, "error")
            g.db.rollback()
        else:
            done("Saved “%s”." % mesh.name, "edit", mesh,
                 {"address": [old_slug, mesh.slug]} if old_slug != mesh.slug else None)
        return redirect(url_for("ui.mesh", ident=mesh.id))
    membership = services.Membership(g.db)
    problems, withheld = publisher.mesh_status(g.db, mesh)
    return render_template(
        "mesh.html", mesh=mesh, pairs={t.id: _pairs(t, membership) for t in mesh.tasks},
        problems=problems, withheld={t.id: errs for t, errs in withheld},
        archives=g.db.query(m.Archive).order_by(m.Archive.name).all(),
        publication=g.db.get(m.Publication, "config/" + mesh.slug),
        friendly=specforms.FRIENDLY_TYPES,
        can_edit=g.access.can("edit", "mesh", mesh), can_delete=g.access.can("delete", "mesh", mesh),
        **access_context("mesh", mesh))


@ui.route("/meshes/<int:ident>/preview")
def mesh_preview(ident):
    mesh = find(m.Mesh, ident)
    b = publisher.Builder(g.db)
    good, _ = b.publishable(mesh.tasks)
    return current_app.response_class(json.dumps(b.mesh_template(mesh, good), indent=2),
                                      mimetype="application/json")


@ui.route("/meshes/<int:ident>/tasks/new", methods=["GET", "POST"])
@ui.route("/meshes/<int:ident>/tasks/<int:task_id>", methods=["GET", "POST"])
def task(ident, task_id=None):
    mesh = find(m.Mesh, ident)
    need("edit", "mesh", mesh)
    task = find(m.MeshTask, task_id) if task_id else m.MeshTask(name="", topology="mesh", enabled=True)
    if task_id and task.mesh_id != mesh.id:
        abort(404)
    errors = []
    if request.method == "POST":
        form = request.form
        name = (form.get("name") or "").strip()
        test = g.db.get(m.Test, form.get("test_id", type=int) or 0)
        group_a = g.db.get(m.HostGroup, form.get("group_a_id", type=int) or 0)
        group_b = g.db.get(m.HostGroup, form.get("group_b_id", type=int) or 0)
        topology = "disjoint" if form.get("topology") == "disjoint" else "mesh"
        if not name:
            errors.append("Give the task a name.")
        elif any(t.name == name and t.id != task.id for t in mesh.tasks):
            errors.append("This mesh already has a task called “%s”." % name)
        if test is None:
            errors.append("Choose a test (start typing its name).")
        if group_a is None:
            errors.append("Choose a host group.")
        if topology == "disjoint" and group_b is None:
            errors.append("A two-group task needs a second host group.")
        if not errors:
            before = None if not task.id else {"test": task.test.name, "a": task.group_a.name,
                                               "b": task.group_b.name if task.group_b else None,
                                               "topology": task.topology, "name": task.name}
            task.name, task.test, task.topology = name, test, topology
            task.group_a, task.group_b = group_a, group_b if topology == "disjoint" else None
            task.enabled = form.get("enabled") == "on"
            if not task.id:
                task.position = max([t.position for t in mesh.tasks] + [-1]) + 1
                mesh.tasks.append(task)
            after = {"test": test.name, "a": group_a.name, "b": task.group_b.name if task.group_b else None,
                     "topology": topology, "name": name}
            done("Saved task “%s”." % name, "edit-task" if before else "add-task", mesh,
                 {"task": name, "before": before, "after": after})
            return redirect(url_for("ui.mesh", ident=mesh.id))
        picked = {"test": test, "group_a": group_a, "group_b": group_b, "name": name, "topology": topology,
                  "enabled": form.get("enabled") == "on"}
    else:
        picked = {"test": task.test, "group_a": task.group_a, "group_b": task.group_b, "name": task.name,
                  "topology": task.topology, "enabled": task.enabled}
    return render_template("task.html", mesh=mesh, task=task, picked=picked, errors=errors), (400 if errors else 200)


@ui.route("/meshes/<int:ident>/tasks/<int:task_id>/<action>", methods=["POST"])
def task_action(ident, task_id, action):
    mesh, task = find(m.Mesh, ident), find(m.MeshTask, task_id)
    need("edit", "mesh", mesh)
    if task.mesh_id != mesh.id:
        abort(404)
    if action == "toggle":
        task.enabled = not task.enabled
        done("Task “%s” %s." % (task.name, "enabled" if task.enabled else "disabled"),
             "enable-task" if task.enabled else "disable-task", mesh, {"task": task.name})
    elif action == "delete":
        name = task.name
        mesh.tasks.remove(task)
        done("Deleted task “%s”." % name, "delete-task", mesh, {"task": name})
    elif action in ("up", "down"):
        ordered = list(mesh.tasks)
        i = ordered.index(task)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(ordered):
            ordered[i], ordered[j] = ordered[j], ordered[i]
            for pos, t in enumerate(ordered):
                t.position = pos
            done("Moved “%s”." % task.name, republish=False)
    else:
        abort(404)
    return redirect(url_for("ui.mesh", ident=mesh.id))


# --------------------------------------------------------------------------
# people, teams, grants
# --------------------------------------------------------------------------

@ui.route("/access")
def access():
    users = g.db.query(m.User).order_by(m.User.name).all()
    teams = g.db.query(m.Team).order_by(m.Team.name).all()
    wide = {scope: [(gr, p) for gr, p in authz.grants_on(g.db, scope) if gr.scope_id is None]
            for scope in authz.SCOPES}
    counts = dict(g.db.query(m.Grant.principal_id, func.count())
                  .filter(m.Grant.principal_type == "user").group_by(m.Grant.principal_id).all())
    return render_template("access.html", users=users, teams=teams, wide=wide, grant_counts=counts,
                           roles={s: authz.roles_for(s, True) for s in authz.SCOPES},
                           role_help=authz.ROLE_HELP)


def _principal(value):
    kind, _, ident = (value or "").partition(":")
    model = {"user": m.User, "team": m.Team}.get(kind)
    return g.db.get(model, int(ident)) if model and ident.isdigit() else None


@ui.route("/access/grants", methods=["POST"])
def grant_add():
    form = request.form
    scope_type = form.get("scope_type")
    if scope_type not in authz.SCOPES:
        abort(400)
    obj = None
    if form.get("scope_id"):
        obj = find(m.SCOPE_MODELS[scope_type], form.get("scope_id", type=int))
    if not (g.user.is_admin or (obj is not None and g.access.can("grant", scope_type, obj))):
        abort(403, "Only an owner of this item or a service administrator can change who has access.")
    principal = _principal(form.get("principal"))
    role = form.get("role")
    if principal is None:
        flash("Choose a person or a team (start typing a name).", "error")
    elif role not in authz.roles_for(scope_type, obj is None):
        flash("That role does not apply here.", "error")
    else:
        authz.grant(g.db, principal, role, scope_type, obj, by=g.user)
        target = obj.label if obj is not None else "all " + authz.SCOPES[scope_type].lower()
        done("%s is now %s of %s." % (getattr(principal, "label", None) or principal.name, role, target),
             "grant", obj, {"to": getattr(principal, "label", None) or principal.name, "role": role,
                            "scope": target}, republish=False)
    return back("ui.access")


@ui.route("/access/grants/<int:grant_id>/delete", methods=["POST"])
def grant_delete(grant_id):
    gr = find(m.Grant, grant_id)
    obj = g.db.get(m.SCOPE_MODELS[gr.scope_type], gr.scope_id) if gr.scope_id else None
    if not (g.user.is_admin or (obj is not None and g.access.can("grant", gr.scope_type, obj))):
        abort(403)
    services.audit(g.db, g.user, "revoke", obj, {"role": gr.role, "principal": "%s:%s" % (
        gr.principal_type, gr.principal_id)}, object_type=gr.scope_type)
    g.db.delete(gr)
    done("Access removed.", republish=False)
    return back("ui.access")


@ui.route("/access/teams", methods=["POST"])
def team_new():
    if not g.user.is_admin:
        abort(403)
    name = (request.form.get("name") or "").strip()
    if not name or g.db.query(m.Team).filter(func.lower(m.Team.name) == name.lower()).first():
        flash("Give the team a name that is not already used.", "error")
        return redirect(url_for("ui.access"))
    team = m.Team(name=name, description=(request.form.get("description") or "").strip())
    g.db.add(team)
    g.db.flush()
    done("Created team “%s”." % name, "create-team", object_type="team", republish=False)
    return redirect(url_for("ui.team", ident=team.id))


@ui.route("/access/teams/<int:ident>", methods=["GET", "POST"])
def team(ident):
    team = find(m.Team, ident)
    can_manage = g.access.can_manage_team(team)
    if request.method == "POST":
        if not can_manage:
            abort(403)
        action = request.form.get("action")
        target = g.db.get(m.User, request.form.get("user_id", type=int) or 0)
        member = next((tm for tm in team.members if target and tm.user_id == target.id), None)
        if action == "add" and target and not member:
            team.members.append(m.TeamMember(user=target))
            done("Added %s to %s." % (target.label, team.name), "team-add", object_type="team",
                 detail={"team": team.name, "user": target.label}, republish=False)
        elif action == "remove" and member:
            team.members.remove(member)
            done("Removed %s from %s." % (target.label, team.name), "team-remove", object_type="team",
                 detail={"team": team.name, "user": target.label}, republish=False)
        elif action == "manager" and member:
            member.is_manager = not member.is_manager
            done("%s %s manage this team." % (target.label, "can now" if member.is_manager else "can no longer"),
                 "team-manager", object_type="team", detail={"team": team.name, "user": target.label},
                 republish=False)
        elif action == "delete" and g.user.is_admin:
            g.db.query(m.Grant).filter_by(principal_type="team", principal_id=team.id).delete()
            name = team.name
            g.db.delete(team)
            done("Deleted team “%s”." % name, "delete-team", object_type="team", republish=False)
            return redirect(url_for("ui.access"))
        elif action == "add":
            flash("Choose a person (start typing a name).", "error")
        return redirect(url_for("ui.team", ident=team.id))
    grants = g.db.query(m.Grant).filter_by(principal_type="team", principal_id=team.id).all()
    return render_template("team.html", team=team, can_manage=can_manage, held=_describe(grants))


def _describe(grants):
    out = []
    for gr in grants:
        obj = g.db.get(m.SCOPE_MODELS[gr.scope_type], gr.scope_id) if gr.scope_id else None
        if gr.scope_id and obj is None:
            continue
        out.append((gr, obj))
    out.sort(key=lambda x: (x[0].scope_type, x[1].label if x[1] is not None else ""))
    return out


@ui.route("/access/users/<int:ident>", methods=["GET", "POST"])
def user_page(ident):
    person = find(m.User, ident)
    if request.method == "POST":
        if not g.user.is_admin:
            abort(403)
        action = request.form.get("action")
        if action == "save":
            if person.id == g.user.id and request.form.get("is_admin") != "on":
                flash("You cannot remove your own administrator role.", "error")
            else:
                person.is_admin = request.form.get("is_admin") == "on"
                person.active = request.form.get("active") == "on" or person.id == g.user.id
                done("Saved %s." % person.label, "edit-user", object_type="user", label=None,
                     detail={"user": person.label, "admin": person.is_admin, "active": person.active},
                     republish=False)
        elif action == "merge":
            target = g.db.get(m.User, request.form.get("target_id", type=int) or 0)
            if target is None or target.id == person.id:
                flash("Choose the account to move this access to.", "error")
            else:
                label = person.label
                authz.merge_users(g.db, person, target)
                done("Moved all access of %s to %s." % (label, target.label), "merge-user",
                     object_type="user", detail={"from": label, "to": target.label}, republish=False)
                return redirect(url_for("ui.user_page", ident=target.id))
        return redirect(url_for("ui.user_page", ident=person.id))
    grants = g.db.query(m.Grant).filter_by(principal_type="user", principal_id=person.id).all()
    return render_template("user.html", person=person, held=_describe(grants))


# --------------------------------------------------------------------------
# published files (Apache serves these directly in production)
# --------------------------------------------------------------------------

def _published(rel):
    root = current_app.config["PUBLISH_DIR"]
    if not os.path.isfile(os.path.join(root, rel)):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(root, rel, mimetype="application/json", max_age=0)


@pub.route("/config")
def config_index():
    return _published("index.json")


@pub.route("/config/<slug>")
def config(slug):
    return _published("config/" + slug)


@pub.route("/auto/<hostname>")
def auto(hostname):
    return _published("auto/" + hostname)


# --------------------------------------------------------------------------
# development sign-in (PWA_AUTH=dev only)
# --------------------------------------------------------------------------

@dev.route("/login", methods=["GET", "POST"])
def login():
    if current_app.config["AUTH"] != "dev":
        abort(404)
    db = g.db
    if db.query(m.User).filter_by(subject="dev:admin").first() is None:
        db.add(m.User(subject="dev:admin", name="Local administrator", is_admin=True, idp="development"))
        db.commit()
    if request.method == "POST":
        person = db.get(m.User, request.form.get("uid", type=int) or 0)
        if person is not None:
            cookie.clear()
            cookie["uid"] = person.id
            return redirect(url_for("ui.dashboard"))
    q = (request.args.get("q") or "").strip()
    query = db.query(m.User)
    if q:
        query = query.filter(or_(m.User.name.ilike("%" + q + "%"), m.User.subject.ilike("%" + q + "%")))
    return render_template("dev_login.html", people=query.order_by(m.User.is_admin.desc(), m.User.name).limit(60).all(), q=q)
