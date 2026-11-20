"""Operations that span several objects: membership, usage, cascades, audit."""
import re

from sqlalchemy import or_
from sqlalchemy.orm import object_session

from . import models as m

RULE_FIELDS = {
    "hostname": "Host name",
    "site": "Site name",
    "communities": "Lookup Service community",
    "capabilities": "Offers test type",
    "source": "Source (lookup / manual)",
}
RULE_OPS = {
    "is": "is",
    "contains": "contains",
    "startswith": "starts with",
    "endswith": "ends with",
    "matches": "matches regular expression",
}


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

def audit(session, actor, action, obj=None, detail=None, object_type=None, label=None):
    session.add(m.AuditLog(
        actor=getattr(actor, "label", actor) or "system", action=action,
        object_type=object_type or getattr(obj, "object_type", None),
        object_id=getattr(obj, "id", None),
        object_label=label or getattr(obj, "label", None), detail=detail))


# --------------------------------------------------------------------------
# Host group membership
# --------------------------------------------------------------------------

def _value(host, field):
    if field.startswith("info."):
        return (host.info or {}).get(field[5:])
    return getattr(host, field, None)


def _test(value, op, wanted):
    wanted = str(wanted or "").lower()
    values = value if isinstance(value, list) else [value]
    for v in values:
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        v = "" if v is None else str(v).lower()
        if ((op == "is" and v == wanted) or (op == "contains" and wanted in v) or
                (op == "startswith" and v.startswith(wanted)) or
                (op == "endswith" and v.endswith(wanted))):
            return True
        if op == "matches":
            try:
                if re.search(wanted, v):
                    return True
            except re.error:
                return False
    return False


def rule_matches(rule, host):
    conditions = (rule or {}).get("conditions") or []
    if not conditions:
        return False
    results = (_test(_value(host, c.get("field", "")), c.get("op"), c.get("value")) for c in conditions)
    return any(results) if rule.get("match") == "any" else all(results)


def validate_rule(rule):
    errors = []
    for c in (rule or {}).get("conditions") or []:
        if c.get("field") not in RULE_FIELDS and not str(c.get("field", "")).startswith("info."):
            errors.append("unknown field %r" % c.get("field"))
        if c.get("op") not in RULE_OPS:
            errors.append("unknown comparison %r" % c.get("op"))
        if c.get("op") == "matches":
            try:
                re.compile(str(c.get("value") or ""))
            except re.error as e:
                errors.append("bad regular expression %r: %s" % (c.get("value"), e))
    return errors


class Membership:
    """Resolves effective members, caching per instance (one request or publish run)."""

    def __init__(self, session):
        self.session = session
        self._cache = {}
        self._all_hosts = None

    def all_hosts(self):
        if self._all_hosts is None:
            self._all_hosts = self.session.query(m.Host).order_by(m.Host.hostname).all()
        return self._all_hosts

    def of(self, group, _path=()):
        """Effective members of a group, sorted by host name."""
        if group is None:
            return []
        if group.id in self._cache:
            return self._cache[group.id]
        if group.id in _path:           # inclusion loop: stop here
            return []
        found = {h.id: h for h in group.members}
        for child in group.includes:
            for h in self.of(child, _path + (group.id,)):
                found[h.id] = h
        if group.rule:
            for h in self.all_hosts():
                if rule_matches(group.rule, h):
                    found[h.id] = h
        for h in group.excludes:
            found.pop(h.id, None)
        result = sorted(found.values(), key=lambda h: h.hostname)
        if not _path:
            self._cache[group.id] = result
        return result

    def why(self, group):
        """For each effective member, how it got in: {host_id: [reasons]}."""
        reasons = {}
        direct = {h.id for h in group.members}
        for h in self.of(group):
            r = []
            if h.id in direct:
                r.append("added by name")
            for child in group.includes:
                if any(x.id == h.id for x in self.of(child)):
                    r.append("via group “%s”" % child.name)
            if group.rule and rule_matches(group.rule, h):
                r.append("matches the rule")
            reasons[h.id] = r
        return reasons


def would_loop(parent, child):
    """True if including `child` in `parent` would make a group contain itself."""
    if parent.id is not None and parent.id == child.id:
        return True
    stack, seen = [child], set()
    while stack:
        g = stack.pop()
        if g.id in seen:
            continue
        seen.add(g.id)
        if parent.id is not None and g.id == parent.id:
            return True
        stack.extend(g.includes)
    return False


# --------------------------------------------------------------------------
# "Where is this used?"
# --------------------------------------------------------------------------

def tasks_using_group(session, group):
    return (session.query(m.MeshTask)
            .filter(or_(m.MeshTask.group_a_id == group.id, m.MeshTask.group_b_id == group.id))
            .order_by(m.MeshTask.mesh_id, m.MeshTask.position).all())


def groups_containing_group(group):
    """Every group that includes `group`, directly or through other groups."""
    out, stack, seen = [], list(group.included_by), set()
    while stack:
        g = stack.pop()
        if g.id in seen:
            continue
        seen.add(g.id)
        out.append(g)
        stack.extend(g.included_by)
    return sorted(out, key=lambda g: g.name)


def host_usage(session, host, membership=None):
    """Where a host takes part: groups (direct and inherited) and mesh tasks."""
    membership = membership or Membership(session)
    direct = list(host.groups)
    excluded_from = (session.query(m.HostGroup)
                     .filter(m.HostGroup.excludes.any(m.Host.id == host.id)).all())
    effective = [g for g in session.query(m.HostGroup).order_by(m.HostGroup.name)
                 if any(h.id == host.id for h in membership.of(g))]
    ids = {g.id for g in effective}
    tasks = []
    if ids:
        tasks = (session.query(m.MeshTask)
                 .filter(or_(m.MeshTask.group_a_id.in_(ids), m.MeshTask.group_b_id.in_(ids)))
                 .order_by(m.MeshTask.mesh_id, m.MeshTask.position).all())
    meshes = sorted({t.mesh for t in tasks}, key=lambda x: x.name)
    by_rule = [g for g in effective if g.rule and rule_matches(g.rule, host)]
    return {"direct": direct, "effective": effective, "excluded_from": excluded_from,
            "by_rule": by_rule, "tasks": tasks, "meshes": meshes}


def test_usage(session, test):
    return session.query(m.MeshTask).filter(m.MeshTask.test_id == test.id).all()


def archive_usage(session, archive):
    meshes = session.query(m.Mesh).filter(m.Mesh.archives.any(m.Archive.id == archive.id)).all()
    hosts = session.query(m.Host).filter(m.Host.archives.any(m.Archive.id == archive.id)).all()
    return {"meshes": meshes, "hosts": hosts}


# --------------------------------------------------------------------------
# Cascading changes
# --------------------------------------------------------------------------

def remove_host(session, host, actor):
    """Take a host out of every group and delete it, in one step.

    Returns what was touched.  Groups that select the host by rule cannot be
    edited for it; the host is gone from them anyway once it is deleted.
    """
    usage = host_usage(session, host)
    summary = {"hostname": host.hostname,
               "groups": [g.name for g in usage["direct"]],
               "meshes": [x.name for x in usage["meshes"]],
               "tasks": len(usage["tasks"])}
    for g in list(usage["direct"]):
        g.members.remove(host)
    for g in usage["excluded_from"]:
        g.excludes.remove(host)
    audit(session, actor, "remove-host", host, summary)
    session.query(m.Grant).filter_by(scope_type="host", scope_id=host.id).delete()
    session.delete(host)
    return summary


def delete_object(session, obj, actor):
    """Delete a group, test, archive or mesh if nothing depends on it.

    Returns a list of blocking reasons (empty when the object was deleted).
    """
    blockers = []
    if isinstance(obj, m.HostGroup):
        blockers += ["task “%s” in mesh “%s”" % (t.name, t.mesh.name)
                     for t in tasks_using_group(session, obj)]
        blockers += ["group “%s” includes it" % g.name for g in obj.included_by]
    elif isinstance(obj, m.Test):
        blockers += ["task “%s” in mesh “%s”" % (t.name, t.mesh.name)
                     for t in test_usage(session, obj)]
    elif isinstance(obj, m.Archive):
        u = archive_usage(session, obj)
        blockers += ["mesh “%s”" % x.name for x in u["meshes"]]
        blockers += ["host %s" % h.hostname for h in u["hosts"]]
    if blockers:
        return blockers
    audit(session, actor, "delete", obj)
    session.query(m.Grant).filter_by(scope_type=obj.object_type, scope_id=obj.id).delete()
    session.delete(obj)
    return []


def replace_everywhere(session, old, new, actor):
    """Point every task (or mesh/host, for archives) at `new` instead of `old`."""
    n = 0
    if isinstance(old, m.Test):
        for t in test_usage(session, old):
            t.test, n = new, n + 1
    elif isinstance(old, m.HostGroup):
        for t in tasks_using_group(session, old):
            if t.group_a_id == old.id:
                t.group_a = new
            if t.group_b_id == old.id:
                t.group_b = new
            n += 1
    elif isinstance(old, m.Archive):
        u = archive_usage(session, old)
        for owner in u["meshes"] + u["hosts"]:
            owner.archives.remove(old)
            if new not in owner.archives:
                owner.archives.append(new)
            n += 1
    audit(session, actor, "replace", old, {"with": new.label, "places": n})
    return n


# --------------------------------------------------------------------------
# Suggestions (type-ahead)
# --------------------------------------------------------------------------

def suggest_hosts(session, text, test_type=None, exclude_ids=(), limit=25):
    """Hosts whose name, site or community matches every word typed.

    Hosts that offer `test_type` come first; the rest are still listed (marked
    by the caller) because Lookup Service data is often incomplete.
    """
    words = [w for w in (text or "").lower().split() if w]
    q = session.query(m.Host)
    for w in words:
        like = "%" + w.replace("%", r"\%").replace("_", r"\_") + "%"
        q = q.filter(or_(m.Host.hostname.ilike(like, escape="\\"),
                         m.Host.display_name.ilike(like, escape="\\"),
                         m.Host.site.ilike(like, escape="\\")))
    hosts = [h for h in q.order_by(m.Host.hostname).limit(400) if h.id not in set(exclude_ids)]
    first = (words[0] if words else "")

    def rank(h):
        offers = test_type is None or test_type in (h.capabilities or [])
        return (0 if offers else 1, 0 if h.hostname.lower().startswith(first) else 1, h.hostname)
    hosts.sort(key=rank)
    return hosts[:limit]
