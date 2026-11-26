"""Analyses of the configuration as a whole, for the Reports pages.

Everything is computed from the database on request: which host pairs each
enabled task measures, how often, and what that adds up to per host and per
mesh.  Nothing here changes anything.
"""
import collections
import datetime
import re

from . import models as m
from . import specforms, testinfo
from .services import Membership
from .sites import domain_of

STALE_DAYS = 14
DAY = 86400.0
DEFAULT_THROUGHPUT_SECONDS = 10.0     # pScheduler's default throughput duration


def _seconds(iso, default=None):
    try:
        return specforms.iso_to_seconds(iso) if iso else default
    except ValueError:
        return default


class Task:
    """One enabled task with its resolved hosts and measured pairs."""

    def __init__(self, task, membership):
        self.task = task
        self.mesh = task.mesh
        self.test = task.test
        self.type = task.test.type
        self.addressing = testinfo.addressing(self.type)
        a = membership.of(task.group_a)
        b = membership.of(task.group_b) if task.topology == "disjoint" else []
        self.hosts = {h.id: h for h in a + b}
        if self.addressing != "pair":
            self.pairs = []                                 # each host on its own
        elif task.topology == "disjoint":
            self.pairs = [(x.id, y.id) for x in a for y in b if x.id != y.id] + \
                         [(y.id, x.id) for x in a for y in b if x.id != y.id]
        else:
            self.pairs = [(x.id, y.id) for x in a for y in a if x.id != y.id]
        repeat = _seconds(task.test.repeat)
        self.continuous = repeat is None
        self.runs_per_day = 0.0 if self.continuous else DAY / repeat
        self.duration = None
        if self.type == "throughput":
            self.duration = _seconds((task.test.spec or {}).get("duration"), DEFAULT_THROUGHPUT_SECONDS)

    @property
    def measurements(self):
        """Ordered pairs (pair tests) or hosts (each-host tests)."""
        return len(self.pairs) if self.addressing == "pair" else len(self.hosts)


def tasks(session, membership=None):
    membership = membership or Membership(session)
    rows = (session.query(m.MeshTask).join(m.Mesh).filter(m.MeshTask.enabled.is_(True))
            .order_by(m.Mesh.name, m.MeshTask.position).all())
    return [Task(t, membership) for t in rows]


def mesh_summary(all_tasks):
    out = collections.OrderedDict()
    for t in all_tasks:
        s = out.setdefault(t.mesh.id, {"mesh": t.mesh, "tasks": 0, "hosts": set(), "measurements": 0,
                                       "runs_per_day": 0.0, "throughput_hours": 0.0, "streams": 0})
        s["tasks"] += 1
        s["hosts"].update(t.hosts)
        s["measurements"] += t.measurements
        if t.continuous:
            s["streams"] += t.measurements
        else:
            s["runs_per_day"] += t.measurements * t.runs_per_day
        if t.duration:
            s["throughput_hours"] += len(t.pairs) * t.runs_per_day * t.duration / 3600.0
    for s in out.values():
        s["hosts"] = len(s["hosts"])
    return list(out.values())


def host_load(all_tasks):
    """Per host: what it takes part in, and how much of its day throughput tests occupy."""
    load = {}

    def entry(host):
        return load.setdefault(host.id, {"host": host, "tasks": set(), "meshes": set(), "partners": set(),
                                         "throughput_runs": 0.0, "throughput_seconds": 0.0,
                                         "streams": 0, "other_runs": 0.0})
    for t in all_tasks:
        for h in t.hosts.values():
            e = entry(h)
            e["tasks"].add(t.task.id)
            e["meshes"].add(t.mesh.id)
        if t.addressing != "pair":
            for h in t.hosts.values():
                entry(h)["other_runs"] += t.runs_per_day
            continue
        for src, dst in t.pairs:
            for me, other in ((src, dst), (dst, src)):
                e = load[me]
                e["partners"].add(other)
                if t.continuous:
                    e["streams"] += 1
                elif t.duration:
                    e["throughput_runs"] += t.runs_per_day
                    e["throughput_seconds"] += t.runs_per_day * t.duration
                else:
                    e["other_runs"] += t.runs_per_day
    rows = []
    for e in load.values():
        e["tasks"], e["meshes"], e["partners"] = len(e["tasks"]), len(e["meshes"]), len(e["partners"])
        e["throughput_share"] = e["throughput_seconds"] / DAY
        rows.append(e)
    return rows


def _spec_label(test):
    spec = test.spec or {}
    bits = []
    if "ip-version" in spec:
        bits.append("IPv%s" % spec["ip-version"])
    if test.repeat:
        bits.append("every " + test.repeat)
    return ", ".join(bits)


def overlaps(all_tasks, limit=40):
    """Host pairs that more than one task measures with the same test type."""
    by_type = collections.defaultdict(lambda: collections.defaultdict(list))
    for t in all_tasks:
        for pair in t.pairs:
            by_type[t.type][pair].append(t)
    groups = collections.Counter()
    members = {}
    for test_type, pairs in by_type.items():
        for pair, ts in pairs.items():
            if len(ts) < 2:
                continue
            key = (test_type, tuple(sorted({x.task.id for x in ts})))
            groups[key] += 1
            members[key] = sorted({x.task.id: x for x in ts}.values(), key=lambda x: (x.mesh.name, x.task.name))
    out = []
    for (test_type, _), count in groups.most_common(limit):
        ts = members[(test_type, _)]
        out.append({"type": test_type, "pairs": count,
                    "tasks": [(x, _spec_label(x.test)) for x in ts],
                    # one test object, or identical settings in different tests
                    "same_settings": len({(x.type, repr(sorted((x.test.spec or {}).items())), x.test.repeat)
                                          for x in ts}) == 1})
    return out


def capability_gaps(all_tasks):
    """Hosts in a task whose Lookup Service record does not list the task's test type.

    Only hosts the Lookup Service describes (with a non-empty list) are judged.
    """
    out = []
    for t in all_tasks:
        missing = [h for h in t.hosts.values()
                   if h.source == "lookup" and h.capabilities and t.type not in h.capabilities]
        if missing:
            out.append({"task": t, "hosts": sorted(missing, key=lambda h: h.hostname)})
    out.sort(key=lambda x: -len(x["hosts"]))
    return out


def _version(host):
    info = host.info or {}
    return info.get("pshost-toolkitversion") or info.get("pshost-bundle-version")


def _version_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def versions(all_tasks):
    """perfSONAR versions reported by the hosts in use."""
    hosts = {}
    for t in all_tasks:
        hosts.update(t.hosts)
    def release(h):         # "5.2.6-1.el9", "5.2.6-1" and "5.2.6" are one release
        parts = re.findall(r"\d+", _version(h) or "")[:3]
        return ".".join(parts) if parts else "unknown"
    counts = collections.Counter(release(h) for h in hosts.values())
    old = sorted((h for h in hosts.values() if _version(h) and _version_key(_version(h)) < (5,)),
                 key=lambda h: (_version_key(_version(h)), h.hostname))
    rows = sorted(counts.items(), key=lambda kv: (kv[0] != "unknown", _version_key(kv[0])), reverse=True)
    return {"rows": rows, "old": old, "total": len(hosts)}


def stale(all_tasks, now=None):
    now = now or datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(days=STALE_DAYS)
    per_mesh = collections.OrderedDict()
    for t in all_tasks:
        for h in t.hosts.values():
            if h.source == "lookup" and h.last_seen and h.last_seen < cutoff:
                per_mesh.setdefault(t.mesh.id, {"mesh": t.mesh, "hosts": {}})["hosts"][h.id] = h
    return [{"mesh": v["mesh"], "hosts": sorted(v["hosts"].values(), key=lambda h: h.last_seen)}
            for v in per_mesh.values()]


def between(all_tasks, a, b=None):
    """What is measured between host a and host b (or everything host a takes part in)."""
    rows = []
    for t in all_tasks:
        if a.id not in t.hosts or (b is not None and b.id not in t.hosts):
            continue
        if t.addressing != "pair":
            if b is None:
                rows.append({"task": t, "direction": "on its own"})
            continue
        if b is None:
            partners = {y for x, y in t.pairs if x == a.id} | {x for x, y in t.pairs if y == a.id}
            rows.append({"task": t, "direction": "with %d host(s)" % len(partners)})
            continue
        forward, back = (a.id, b.id) in set(t.pairs), (b.id, a.id) in set(t.pairs)
        if forward or back:
            rows.append({"task": t, "direction": "both directions" if forward and back else
                         ("%s → %s" % (a.hostname, b.hostname) if forward else "%s → %s" % (b.hostname, a.hostname))})
    return rows


# --------------------------------------------------------------------------
# Follow-up with sites: silent hosts grouped by who runs them
# --------------------------------------------------------------------------

ACTIONS = {
    "reinstall": "Reinstall with perfSONAR 5.2 (4.x and older get no updates)",
    "update": "Update to perfSONAR 5.2, then check it registers with the Lookup Service",
    "check": "Check the host is up and its Lookup Service registration works (the software is current)",
    "never": "Added by hand and never registered: check it is running, then register it",
    "unknown": "Check the host is up; it never reported a release",
}


def silent_hosts(all_tasks, now=None):
    """Hosts in enabled tasks that the Lookup Service has not listed lately, or ever."""
    now = now or datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(days=STALE_DAYS)
    hosts, meshes = {}, collections.defaultdict(set)
    for t in all_tasks:
        for h in t.hosts.values():
            if (h.last_seen is None) or (h.source == "lookup" and h.last_seen < cutoff):
                hosts[h.id] = h
                meshes[h.id].add(t.mesh.slug)
    return hosts, meshes


def action_for(host):
    v = _version(host)
    if not v:
        return "never" if host.last_seen is None and host.source != "lookup" else "unknown"
    key = _version_key(v)
    if key < (5,):
        return "reinstall"
    if key < (5, 2):
        return "update"
    return "check"


def follow_up(all_tasks, registry, now=None, contacts=None):
    """Silent hosts grouped by site, largest groups first, for writing to sites.

    `contacts` = {site: [SiteContact, newest first]} (pwa.followup.contacts_by_site).
    """
    now = now or datetime.datetime.utcnow()
    hosts, meshes = silent_hosts(all_tasks, now)
    known = (registry or {}).get("hosts") or {}
    groups = {}
    for h in hosts.values():
        reg = known.get(h.hostname) or {}
        reported = h.site if h.site and not h.site.startswith("(") else None   # "(pScheduler at x.org)" is a placeholder
        site = reg.get("site") or reported or "Unknown site (%s)" % domain_of(h.hostname)
        g = groups.setdefault(site, {"site": site, "hosts": [], "reach": [], "experiments": set(),
                                     "country": None, "tier": None, "federation": None, "ngi": None,
                                     "gocdb": None, "registered": False})
        cric, gocdb, osg = reg.get("cric") or {}, reg.get("gocdb") or {}, reg.get("osg") or {}
        g["registered"] = g["registered"] or bool(reg)
        g["country"] = g["country"] or cric.get("country") or gocdb.get("country") or (h.info or {}).get("location-country")
        g["tier"] = g["tier"] or cric.get("tier")
        g["federation"] = g["federation"] or cric.get("federation")
        g["ngi"] = g["ngi"] or gocdb.get("ngi")
        g["gocdb"] = g["gocdb"] or gocdb.get("portal")
        g["experiments"].update(cric.get("experiments") or [])
        for way in reg.get("reach") or []:
            if way not in g["reach"]:
                g["reach"].append(way)
        action = action_for(h)
        days = (now - h.last_seen).days if h.last_seen else None
        g["hosts"].append({"host": h, "release": _version(h), "action": action, "action_text": ACTIONS[action],
                           "silent_days": days, "meshes": sorted(meshes[h.id]),
                           "cric": ", ".join(("%s %s" % (s.get("flavour") or "", s.get("status") or s.get("state") or "")
                                              ).strip() for s in cric.get("services") or []) or None,
                           "matched": reg.get("matched")})
    out = []
    for g in groups.values():
        g["contacts"] = (contacts or {}).get(g["site"], [])
        g["status"] = g["contacts"][0].status if g["contacts"] else None
        g["experiments"] = sorted(g["experiments"])
        g["hosts"].sort(key=lambda x: (x["silent_days"] is None, -(x["silent_days"] or 0), x["host"].hostname))
        out.append(g)
    out.sort(key=lambda g: (-len(g["hosts"]), g["site"].lower()))
    return out
