"""Build pSConfig templates from the database and publish them as files.

Toolkits only ever read, so every template is rendered when something
changes and written to PUBLISH_DIR; the web server hands the files out.

    config/<slug>      one template per mesh
    auto/<hostname>    every task a host takes part in, across all meshes
    index.json         list of mesh URLs (what PWA served at /pub/config)

A file replaces the live one only if it passes validation: the pSConfig
JSON schema, plus pScheduler's schema for each test and archiver.  When it
does not, the previous file stays in place and the reason is recorded.
"""
import datetime
import hashlib
import json
import os
import re
import tempfile

import jsonschema

from . import models as m
from . import specforms
from .services import Membership

_SCHEMA = None


def psconfig_schema():
    global _SCHEMA
    if _SCHEMA is None:
        with open(os.path.join(specforms.SPEC_DIR, "psconfig-schema.json")) as f:
            _SCHEMA = jsonschema.Draft4Validator(json.load(f))
    return _SCHEMA


def _key(text):
    return re.sub(r"[^a-zA-Z0-9:._-]+", "-", text).strip("-") or "x"


def archive_key(archive):
    # Imported archives keep the key PWA published them under.
    if archive.legacy_id:
        return "custom-" + archive.legacy_id
    return "%s-%d" % (_key(archive.name), archive.id)


def schedule_for(test):
    if not test.repeat:
        return None, None
    sched = {"repeat": test.repeat}
    if test.sliprand:
        sched["sliprand"] = True
    if test.slip:
        sched["slip"] = test.slip
    key = "every-" + test.repeat + ("-slip-" + test.slip if test.slip else "")
    return key, sched


class Builder:
    """Assembles templates.  One instance per publish run (it caches membership)."""

    def __init__(self, session, reference=True):
        self.session = session
        self.reference = reference
        self.membership = Membership(session)
        self._spec_errors = {}

    # -- pieces -------------------------------------------------------------

    def address(self, host):
        meta = {"display-url": host.display_url or "https://%s/toolkit/" % host.hostname}
        if host.shown_name:
            meta = dict({"display-name": host.shown_name}, **meta)
        entry = {"address": host.hostname, "host": host.hostname, "_meta": meta}
        if host.no_agent:
            entry["no-agent"] = True
        return entry

    def test_object(self, test):
        spec = dict(test.spec or {})
        spec["source"] = "{% address[0] %}"
        spec["dest"] = "{% address[1] %}"
        spec["schema"] = (specforms.schema_version("tests", test.type, test.spec or {})
                          if specforms.known("tests", test.type) else 1)
        return {"type": test.type, "spec": spec}

    def task_hosts(self, task):
        a = self.membership.of(task.group_a)
        b = self.membership.of(task.group_b) if task.topology == "disjoint" else []
        return a, b

    # -- templates ----------------------------------------------------------

    def assemble(self, display_name, tasks):
        """tasks: list of (key, MeshTask).  Returns a pSConfig template."""
        archives, addresses, groups, tests, schedules, out_tasks, hosts = {}, {}, {}, {}, {}, {}, {}

        def use_archive(a):
            archives.setdefault(archive_key(a), a.definition)
            return archive_key(a)

        def use_host(h):
            if h.hostname not in addresses:
                addresses[h.hostname] = self.address(h)
                keys = [use_archive(a) for a in h.archives]
                hosts[h.hostname] = {"archives": keys} if keys else {}

        for key, task in tasks:
            a, b = self.task_hosts(task)
            for h in a + b:
                use_host(h)
            if task.topology == "disjoint":
                groups[key] = {"type": "disjoint",
                               "a-addresses": [{"name": h.hostname} for h in a],
                               "b-addresses": [{"name": h.hostname} for h in b]}
            else:
                groups[key] = {"type": "mesh", "addresses": [{"name": h.hostname} for h in a]}
            tests[key] = self.test_object(task.test)
            entry = {"group": key, "test": key,
                     "archives": [use_archive(x) for x in task.mesh.archives],
                     "_meta": {"display-name": task.name}}
            skey, sched = schedule_for(task.test)
            if sched:
                schedules[skey] = sched
                entry["schedule"] = skey
            if task.test.tools:
                entry["tools"] = list(task.test.tools)
            if self.reference:
                # What the 5.x Grafana agent reads to name grids and dashboards.
                entry["reference"] = {"display-task-name": task.name,
                                      "display-task-group": [task.mesh.name]}
            out_tasks[key] = entry

        return {"archives": archives, "addresses": addresses, "groups": groups, "tests": tests,
                "schedules": schedules, "tasks": out_tasks,
                "_meta": {"display-name": display_name}, "hosts": hosts}

    def publishable(self, tasks):
        """Split enabled tasks into (those safe to publish, those withheld).

        A task whose test pScheduler would reject is withheld on its own, so
        one bad test cannot take the rest of a mesh, or a host's feed, down.
        """
        good, withheld = [], []
        for t in tasks:
            if t.enabled:
                (withheld if self.spec_errors(t.test) else good).append(t)
        return good, withheld

    def mesh_template(self, mesh, tasks=None):
        tasks = [t for t in mesh.tasks if t.enabled] if tasks is None else tasks
        template = self.assemble(mesh.name, [(t.name, t) for t in tasks])
        for a in mesh.archives:          # listed even when the mesh has no tasks yet
            template["archives"].setdefault(archive_key(a), a.definition)
        return template

    def tasks_by_host(self):
        """{host_id: [MeshTask]} for every enabled task, in mesh order."""
        out = {}
        tasks = (self.session.query(m.MeshTask).join(m.Mesh).filter(m.MeshTask.enabled.is_(True))
                 .order_by(m.Mesh.slug, m.MeshTask.position).all())
        for task in tasks:
            a, b = self.task_hosts(task)
            for hid in {h.id for h in a + b}:
                out.setdefault(hid, []).append(task)
        return out

    def host_template(self, host, tasks):
        names = {}
        for t in tasks:
            names.setdefault(t.name, set()).add(t.mesh_id)
        keyed = [(t.name if len(names[t.name]) == 1 else "%s [%s]" % (t.name, t.mesh.slug), t)
                 for t in tasks]
        return self.assemble("Auto-MeshConfig for " + host.hostname, keyed)

    # -- validation ---------------------------------------------------------

    def spec_errors(self, test):
        if test.id not in self._spec_errors:
            self._spec_errors[test.id] = specforms.validate("tests", test.type, test.spec or {})
        return self._spec_errors[test.id]

    def problems(self, template):
        """Reasons not to publish a template (empty list = publish)."""
        out = []
        for e in psconfig_schema().iter_errors(template):
            out.append("template: %s at /%s" % (e.message[:200], "/".join(str(p) for p in e.path)))
            if len(out) >= 5:
                break
        for key, definition in template["archives"].items():
            kind = (definition or {}).get("archiver")
            if kind and specforms.known("archivers", kind):
                for err in specforms.validate("archivers", kind, (definition or {}).get("data") or {}):
                    out.append("archive %s: %s" % (key, err))
        return out


def mesh_status(session, mesh):
    """(problems that block publishing, [(task, [spec errors])] withheld)."""
    b = Builder(session)
    good, withheld = b.publishable(mesh.tasks)
    return b.problems(b.mesh_template(mesh, good)), [(t, b.spec_errors(t.test)) for t in withheld]


def _dump(obj):
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _write_atomically(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def publish_all(session, out_dir, base_url, reference=True):
    """Render everything; replace the files that changed and are valid.

    Returns {"written", "unchanged", "removed", "failed": [(path, reason)],
    "withheld": [(mesh slug, task name, reason)]}.
    """
    builder = Builder(session, reference=reference)
    now = datetime.datetime.utcnow()
    result = {"written": 0, "unchanged": 0, "removed": 0, "failed": [], "withheld": []}
    state = {p.path: p for p in session.query(m.Publication)}
    wanted = set()

    def emit(rel, template, withheld, check=True):
        wanted.add(rel)
        pub = state.get(rel)
        if pub is None:
            pub = state[rel] = m.Publication(path=rel)
            session.add(pub)
        pub.checked = now
        errors = builder.problems(template) if check else []
        if errors:
            pub.ok, pub.message = False, "\n".join(errors[:20])
            result["failed"].append((rel, errors[0]))
            return
        data = _dump(template)
        digest = hashlib.sha256(data).hexdigest()
        path = os.path.join(out_dir, rel)
        if pub.sha256 == digest and os.path.exists(path):
            result["unchanged"] += 1
        else:
            _write_atomically(path, data)
            pub.sha256, pub.published = digest, now
            result["written"] += 1
        pub.ok = True
        pub.message = "\n".join("withheld task “%s”: %s" % (t.name, "; ".join(builder.spec_errors(t.test)))
                                for t in withheld)

    meshes = session.query(m.Mesh).order_by(m.Mesh.slug).all()
    for mesh in meshes:
        good, withheld = builder.publishable(mesh.tasks)
        for t in withheld:
            result["withheld"].append((mesh.slug, t.name, "; ".join(builder.spec_errors(t.test))))
        emit("config/" + mesh.slug, builder.mesh_template(mesh, good), withheld)

    by_host = builder.tasks_by_host()
    for host in builder.membership.all_hosts():
        good, withheld = builder.publishable(by_host.get(host.id, []))
        # Same assembly code as the mesh files, whose structure was checked
        # above, so the per-host files are not schema-checked a second time.
        emit("auto/" + host.hostname, builder.host_template(host, good), withheld, check=False)

    index = [{"include": ["%s/config/%s" % (base_url.rstrip("/"), mesh.slug)]} for mesh in meshes]
    data = _dump(index)
    index_path = os.path.join(out_dir, "index.json")
    if not os.path.exists(index_path) or open(index_path, "rb").read() != data:
        _write_atomically(index_path, data)

    for rel, pub in list(state.items()):
        if rel not in wanted:
            path = os.path.join(out_dir, rel)
            if os.path.exists(path):
                os.unlink(path)
            session.delete(pub)
            result["removed"] += 1
    session.commit()
    return result
