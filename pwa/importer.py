"""One-time import from pSConfig Web Admin 4.x/5.x.

Input is a directory of `mongoexport --jsonArray` files, one per collection
of PWA's `mca` database (hosts, hostgroups, testspecs, archives, configs);
see docs/MIGRATION.md.  An optional users.json (tools/export_auth_users.py)
names the people behind PWA's numeric admin ids:
[{"id", "fullname", "email", "subs": [...], "idp", "admin", "active"}].

PWA stored test specs in MeshConfig-era form and translated them every time
a config was requested (api/pub/meshconfig.js).  `translate_testspec` is that
translation done once, at import, so the stored spec is what pScheduler
reads.  tests/test_import_compat.py checks the result against what the
running PWA publishes.
"""
import datetime
import json
import os
import re

from . import models as m
from .specforms import iso_to_seconds, seconds_to_iso

SERVICE_TO_TEST = {"bwctl": "throughput", "owamp": "latencybg", "ping": "rtt",
                   "traceroute": "trace"}
SERVICE_TO_CAPABILITIES = {"bwctl": ["throughput"], "owamp": ["latency", "latencybg"],
                           "ping": ["rtt"], "traceroute": ["trace"]}
TOOL_NAMES = {"bwctl/nuttcp": "nuttcp", "bwctl/iperf": "iperf", "bwctl/iperf3": "iperf3"}
_NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?)$")


def _plain(value):
    """Strip MongoDB Extended JSON wrappers."""
    if isinstance(value, dict):
        if len(value) == 1:
            (k, v), = value.items()
            if k == "$oid":
                return v
            if k == "$date":
                return v if isinstance(v, str) else _plain(v)
            if k in ("$numberInt", "$numberLong"):
                return int(v)
            if k == "$numberDouble":
                return float(v)
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _date(text):
    if not text:
        return None
    try:
        return datetime.datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _truthy(v):
    return bool(v) and v == v      # JS truthiness for the values that occur here


def _iso(value):
    """PWA's seconds_to_iso8601()."""
    if value is None:
        return "P0D"
    if isinstance(value, str) and value.strip() == "":
        return "PT0S"
    return seconds_to_iso(float(value))


def _js_number(value):
    """What `!isNaN(v) ? Number(v) : v` does to a spec value."""
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        return value
    if value is None:
        return 0
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return 0
        if not _NUMERIC.match(s):
            return value
        value = float(s)
    if isinstance(value, float) and value == int(value):
        return int(value)
    return value


def _rename(spec, old, new):
    if old in spec:
        spec[new] = spec.pop(old)


def translate_testspec(service_type, specs, schedule_type):
    """PWA testspec -> native test definition.

    Returns dict(type, spec, tools, repeat, slip, sliprand, notes).  Follows
    meshconfig_testspec_to_psconfig() and the task loop in
    _process_published_config() step by step.
    """
    notes = []
    original_tool = (specs or {}).get("tool")
    spec = {k.replace("_", "-"): v for k, v in (specs or {}).items()}
    ttype = SERVICE_TO_TEST.get(service_type, service_type)
    stored = schedule_type or service_type      # what the task loop tests
    effective = stored                          # what the translation recomputes
    include = stored != "continuous"

    for field in ("duration", "interval", "test-interval", "report-interval",
                  "waittime", "slip", "timeout"):
        if _truthy(spec.get(field)):
            spec[field] = _iso(spec[field])
    _rename(spec, "interval", "test-interval")
    for old, new in (("sample-count", "packet-count"), ("udp-bandwidth", "bandwidth"),
                     ("tcp-bandwidth", "bandwidth"), ("waittime", "sendwait"),
                     ("timeout", "wait"), ("tos-bits", "ip-tos"), ("omit-interval", "omit")):
        _rename(spec, old, new)
    if "omit" in spec:
        spec["omit"] = _iso(spec["omit"])

    if ttype == "rtt":
        if "packet-interval" in spec:
            spec["packet-interval"] = _iso(spec["packet-interval"])
        _rename(spec, "packet-count", "count")
        _rename(spec, "packet-interval", "interval")
        _rename(spec, "packet-size", "length")
    elif ttype == "trace":
        _rename(spec, "packet-size", "length")
        if not _truthy(spec.get("probe-type")):
            spec.pop("probe-type", None)
        spec.pop("protocol", None)
    elif ttype == "throughput":
        if "probe-type" in spec and "protocol" not in spec and spec["probe-type"] == "udp":
            spec["udp"] = True
        spec.pop("probe-type", None)
    if ttype == "throughput" and "streams" in spec:
        # PWA before late 2024 saved the stream count as "streams" (PWA issue #234).
        if "parallel" not in spec:
            spec["parallel"] = spec["streams"]
            notes.append("“streams” renamed to “parallel” (PWA issue #234)")
        del spec["streams"]
    spec.pop("tool", None)
    spec.pop("force-bidirectional", None)

    for key in list(spec):
        spec[key] = _js_number(spec[key])

    repeat = slip = None
    interval = spec.get("test-interval")
    if _truthy(interval):
        effective, include, repeat = "interval", True, interval
    if include and repeat and effective != "continuous":
        if "slip" in spec and spec["slip"] != 0:
            slip = spec["slip"]
        else:
            slip = seconds_to_iso(iso_to_seconds(repeat) / 2)
    spec.pop("slip", None)
    spec.pop("random-start-percentage", None)
    if spec.get("protocol") == "udp":
        spec["udp"] = True
    spec.pop("protocol", None)
    if "ipv4-only" in spec:
        spec["ip-version"] = 4
        del spec["ipv4-only"]
    if "ipv6-only" in spec:
        spec["ip-version"] = 6
        del spec["ipv6-only"]
    _rename(spec, "report-interval", "interval")
    spec.pop("type", None)

    # back in the task loop
    task_has_schedule = True
    if ttype == "latencybg":
        if stored == "interval":
            ttype = "latency"
            spec.pop("duration", None)
        else:
            task_has_schedule = False
    spec.pop("test-interval", None)

    if not task_has_schedule:
        if repeat:
            notes.append("continuous test: the stored interval (%s) was never used" % repeat)
        repeat = slip = None
    elif include and not repeat:
        notes.append("no interval set: PWA published this task with a schedule that does not exist")

    tools = []
    if task_has_schedule and _truthy(original_tool):
        tools = [TOOL_NAMES.get(original_tool, original_tool)]
    return {"type": ttype, "spec": spec, "tools": tools, "repeat": repeat, "slip": slip,
            "sliprand": True, "notes": notes}


class Report:
    def __init__(self):
        self.counts = {}
        self.warnings = []

    def count(self, what, n=1):
        self.counts[what] = self.counts.get(what, 0) + n

    def warn(self, text):
        self.warnings.append(text)

    def __str__(self):
        lines = ["imported: " + ", ".join("%d %s" % (v, k) for k, v in self.counts.items())]
        lines += ["warning: " + w for w in self.warnings]
        return "\n".join(lines)


def _archive_definition(doc):
    """PWA archive document -> pSConfig archive object (pub_shared.format_archive)."""
    kind, data = doc.get("archiver"), dict(doc.get("data") or {})
    if kind == "rawjson":
        return json.loads(data.get("archiver_custom_json") or "{}")
    if kind == "esmond":
        out = {"url": data.get("_url"), "measurement-agent": "{% scheduled_by_address %}"}
        if data.get("verify_ssl"):
            out["verify-ssl"] = data["verify_ssl"]
        return {"archiver": "esmond", "data": out}
    if kind == "rabbitmq":
        url, user, pw = data.get("_url"), data.pop("_username", None), data.pop("_password", None)
        if url and (user or pw) and re.match(r"^amqps?://", url):
            data["_url"] = re.sub(r"^(amqps?://)(.+)$",
                                  lambda mo: "%s%s:%s@%s" % (mo.group(1), user or "", pw or "", mo.group(2)), url)
        if "connection_lifetime" in data:
            data["connection-expires"] = _iso(data.pop("connection_lifetime"))
            data["schema"] = 2
        _rename(data, "exchange_key", "exchange")
        data = {(k if k == "_url" else k.replace("_", "-")): v for k, v in data.items()}
        return {"archiver": "rabbitmq", "data": data}
    return {"archiver": kind, "data": data}


def _esmond(url):
    return {"archiver": "esmond", "data": {"url": url, "measurement-agent": "{% scheduled_by_address %}",
                                           "schema": 1}}


def import_pwa(session, export_dir):
    """Load a PWA export into an empty database.  Returns a Report."""
    rep = Report()

    def load(name, required=True):
        path = os.path.join(export_dir, name + ".json")
        if not os.path.exists(path):
            if required:
                raise FileNotFoundError(path)
            return []
        with open(path, encoding="utf-8") as f:
            return _plain(json.load(f))

    people = {str(u["id"]): u for u in load("users", required=False)}
    users = {}

    taken = set()

    def user_for(pwa_id):
        pwa_id = str(pwa_id)
        if pwa_id not in users:
            p = people.get(pwa_id, {})
            subs = [s for s in p.get("subs") or [] if s and s not in taken]
            taken.update(subs)
            user = m.User(subject=subs[0] if subs else "pwa:" + pwa_id,
                          name=p.get("fullname") or "PWA user #" + pwa_id, email=p.get("email") or None,
                          idp=p.get("idp") or "imported from PWA", is_admin=bool(p.get("admin")),
                          active=p.get("active", True) is not False)
            user.identities = [m.UserIdentity(subject=s) for s in subs[1:]]
            session.add(user)
            users[pwa_id] = user
            rep.count("users")
            if subs:
                rep.count("users with a known federated login")
        return users[pwa_id]

    pending_grants = []

    def own(obj, admins):
        for a in admins or []:
            if a is not None:
                pending_grants.append((user_for(a), obj))

    for pwa_id, p in people.items():         # service administrators own nothing but must carry over
        if p.get("admin"):
            user_for(pwa_id)

    # archives -------------------------------------------------------------
    archives = {}
    for doc in load("archives"):
        try:
            definition = _archive_definition(doc)
        except ValueError as e:
            rep.warn("archive %r: custom JSON does not parse (%s); skipped" % (doc.get("name"), e))
            continue
        a = m.Archive(name=doc.get("name") or doc["_id"], definition=definition,
                      description=_desc(doc, "New Archive"), legacy_id=doc["_id"])
        session.add(a)
        archives[doc["_id"]] = a
        own(a, doc.get("admins"))
        rep.count("archives")

    by_url = {}

    def esmond_archive(url):
        if url not in by_url:
            by_url[url] = m.Archive(name="esmond " + re.sub(r"^https?://([^/]+).*", r"\1", url),
                                    definition=_esmond(url),
                                    description="Imported from a PWA measurement-archive URL. "
                                                "pScheduler 5.3 rejects the esmond archiver.")
            session.add(by_url[url])
            rep.count("archives")
        return by_url[url]

    # hosts ----------------------------------------------------------------
    hosts = {}
    for doc in load("hosts"):
        caps = []
        for s in doc.get("services") or []:
            for c in SERVICE_TO_CAPABILITIES.get(s.get("type"), []):
                if c not in caps:
                    caps.append(c)
        from_ls = bool(doc.get("lsid"))
        url = doc.get("toolkit_url")
        h = m.Host(hostname=doc["hostname"], display_name=doc.get("desc") or None,
                   site=doc.get("sitename") or None,
                   display_url=url if url and url != "auto" else None,
                   source="lookup" if from_ls else "manual",
                   communities=doc.get("communities") or [], capabilities=caps,
                   addresses=[{"address": a.get("address"), "family": a.get("family")}
                              for a in doc.get("addresses") or []],
                   info=doc.get("info") or {}, no_agent=bool(doc.get("no_agent")),
                   last_seen=_date(doc.get("update_date")) if from_ls else None,
                   legacy_id=doc["_id"])
        seen = set()
        for aid in doc.get("additional_archives") or []:
            if aid in archives and aid not in seen:
                h.archives.append(archives[aid])
                seen.add(aid)
            elif aid not in archives:
                rep.count("references to deleted archives dropped")
        if doc.get("local_ma"):
            h.archives.append(esmond_archive(doc.get("local_ma_url") or
                                             "https://%s/esmond/perfsonar/archive" % doc["hostname"]))
            rep.warn("host %s archives to its own esmond; pScheduler 5.3 rejects esmond" % doc["hostname"])
        for url in doc.get("ma_urls") or []:
            if url:
                h.archives.append(esmond_archive(url))
        session.add(h)
        hosts[doc["_id"]] = h
        if not from_ls:
            own(h, doc.get("admins"))
        rep.count("hosts")

    # host groups ----------------------------------------------------------
    groups = {}
    for doc in load("hostgroups"):
        g = m.HostGroup(name=doc.get("name") or doc["_id"], description=_desc(doc, "New Hostgroup"),
                        intended_test=SERVICE_TO_TEST.get(doc.get("service_type")),
                        legacy_id=doc["_id"])
        missing = 0
        for hid in doc.get("hosts") or []:
            if hid in hosts:
                if hosts[hid] not in g.members:
                    g.members.append(hosts[hid])
            else:
                missing += 1
        if missing:
            rep.warn("host group %r: %d member(s) no longer exist; dropped" % (g.name, missing))
        if doc.get("type") == "dynamic":
            g.description = (g.description + "\nImported from a dynamic PWA group; its members were "
                             "frozen as of the import. Original filter:\n" + (doc.get("host_filter") or "")).strip()
            rep.warn("host group %r was dynamic: members frozen, rule must be re-created" % g.name)
        session.add(g)
        groups[doc["_id"]] = g
        own(g, doc.get("admins"))
        rep.count("host groups")

    # tests ----------------------------------------------------------------
    tests = {}
    for doc in load("testspecs"):
        t = translate_testspec(doc.get("service_type"), doc.get("specs"), doc.get("schedule_type"))
        test = m.Test(name=doc.get("name") or doc["_id"], description=_desc(doc, "New Testspec"),
                      type=t["type"], spec=t["spec"], tools=t["tools"], repeat=t["repeat"],
                      slip=t["slip"], sliprand=t["sliprand"], legacy_id=doc["_id"])
        for note in t["notes"]:
            rep.warn("test %r: %s" % (test.name, note))
        session.add(test)
        tests[doc["_id"]] = test
        own(test, doc.get("admins"))
        rep.count("tests")

    # meshes ---------------------------------------------------------------
    for doc in load("configs"):
        mesh = m.Mesh(slug=doc["url"], name=doc.get("name") or doc["url"],
                      description=doc.get("desc") or "", legacy_id=doc["_id"])
        for aid in doc.get("archives") or []:
            if aid in archives and archives[aid] not in mesh.archives:
                mesh.archives.append(archives[aid])
        for url in doc.get("ma_urls") or []:
            if url:
                mesh.archives.append(esmond_archive(url))
                rep.warn("mesh %r archives to esmond %s; pScheduler 5.3 rejects esmond" % (mesh.name, url))
        if (doc.get("ma_custom_json") or "").strip():
            rep.warn("mesh %r has custom archive JSON; re-create it as an archive" % mesh.name)
        if doc.get("force_endpoint_mas"):
            rep.warn("mesh %r forced archiving to every host's own esmond; not carried over" % mesh.name)
        names = set()
        for pos, t in enumerate(doc.get("tests") or []):
            name = t.get("name") or "task %d" % (pos + 1)
            where = "mesh %r task %r" % (mesh.name, name)
            if t.get("testspec") not in tests or t.get("agroup") not in groups:
                rep.warn("%s refers to a deleted test or host group; skipped" % where)
                continue
            topology = t.get("mesh_type") or "mesh"
            if topology not in ("mesh", "disjoint"):
                rep.warn("%s has unsupported topology %r; skipped" % (where, topology))
                continue
            if name in names:
                rep.warn("%s: duplicate task name in this mesh; renamed" % where)
                name = "%s (%d)" % (name, pos + 1)
            names.add(name)
            group_a, group_b = groups[t["agroup"]], groups.get(t.get("bgroup"))
            if topology == "mesh" and group_b is not None and group_b is not group_a:
                # PWA put both groups' hosts into one full mesh.  Keep that
                # behaviour explicit with a group that includes both.
                merged = m.HostGroup(name="%s: %s + %s" % (mesh.name, group_a.name, group_b.name),
                                     description="Created by the PWA import: this mesh task named "
                                                 "two host groups and PWA tested all of them together.",
                                     intended_test=group_a.intended_test)
                merged.includes = [group_a, group_b]
                session.add(merged)
                rep.warn("%s is a full mesh over two groups; combined into %r" % (where, merged.name))
                group_a, group_b = merged, None
            if topology == "mesh":
                group_b = None
            elif group_b is None:
                rep.warn("%s is disjoint but has no B group; skipped" % where)
                continue
            if t.get("nahosts"):
                rep.warn("%s lists no-agent hosts per task; set no-agent on the hosts instead" % where)
            mesh.tasks.append(m.MeshTask(position=pos, name=name, topology=topology,
                                         test=tests[t["testspec"]], group_a=group_a, group_b=group_b,
                                         enabled=t.get("enabled", True) is not False))
            rep.count("tasks")
        session.add(mesh)
        own(mesh, doc.get("admins"))
        rep.count("meshes")

    session.flush()
    for user, obj in pending_grants:
        session.add(m.Grant(principal_type="user", principal_id=user.id, role="owner",
                            scope_type=obj.object_type, scope_id=obj.id, created_by="import"))
    rep.count("grants", len(pending_grants))
    session.add(m.AuditLog(actor="import", action="import", object_type="system",
                           object_label=os.path.basename(os.path.normpath(export_dir)),
                           detail={"counts": rep.counts, "warnings": len(rep.warnings)}))
    session.commit()
    return rep


def _desc(doc, placeholder):
    d = (doc.get("desc") or "").strip()
    return "" if d == placeholder else d
