"""Compare two pSConfig templates by meaning rather than by text.

Archive, schedule, group and test keys are internal names: two templates
that wire the same hosts to the same test, schedule and archives are the
same to an agent whatever those keys are.  `canonical()` resolves every
reference so the comparison is on what each task actually does.
"""
import json


def _j(obj):
    return json.dumps(obj, sort_keys=True)


def canonical(template):
    archives = template.get("archives") or {}
    schedules = template.get("schedules") or {}
    groups = template.get("groups") or {}
    tests = template.get("tests") or {}

    def group(key):
        g = groups.get(key) or {}
        out = {"type": g.get("type")}
        for field in ("addresses", "a-addresses", "b-addresses"):
            if field in g:
                out[field] = sorted(a.get("name") for a in g[field])
        return out

    tasks = {}
    for name, t in (template.get("tasks") or {}).items():
        entry = {k: v for k, v in t.items() if k not in ("group", "test", "schedule", "archives")}
        entry["group"] = group(t.get("group"))
        entry["test"] = tests.get(t.get("test"))
        entry["schedule"] = schedules.get(t["schedule"], "MISSING SCHEDULE") if t.get("schedule") else None
        entry["archives"] = sorted(_j(archives.get(a, "MISSING ARCHIVE")) for a in t.get("archives") or [])
        tasks[name] = entry
    hosts = {name: sorted(_j(archives.get(a, "MISSING ARCHIVE")) for a in (h or {}).get("archives") or [])
             for name, h in (template.get("hosts") or {}).items()}
    return {"_meta": template.get("_meta"), "addresses": template.get("addresses") or {},
            "hosts": hosts, "tasks": tasks}


def differences(reference, candidate, limit=12):
    """Human-readable differences between two templates (empty list = same)."""
    a, b = canonical(reference), canonical(candidate)
    out = []

    def walk(x, y, path):
        if len(out) >= limit or x == y:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y), key=str):
                if k not in x:
                    out.append("%s/%s: only in candidate: %s" % (path, k, _j(y[k])[:140]))
                elif k not in y:
                    out.append("%s/%s: only in reference: %s" % (path, k, _j(x[k])[:140]))
                else:
                    walk(x[k], y[k], "%s/%s" % (path, k))
                if len(out) >= limit:
                    return
        elif isinstance(x, list) and isinstance(y, list) and all(isinstance(i, str) for i in x + y):
            only_x, only_y = sorted(set(x) - set(y)), sorted(set(y) - set(x))
            out.append("%s: reference-only %s; candidate-only %s" % (
                path, _j(only_x)[:160], _j(only_y)[:160]))
        else:
            out.append("%s: reference %s != candidate %s" % (path, _j(x)[:120], _j(y)[:120]))
    walk(a, b, "")
    return out
