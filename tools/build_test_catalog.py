#!/usr/bin/env python3
"""Ask a real pScheduler which tools support which test options.

Usage: build_test_catalog.py [--pscheduler https://localhost/pscheduler] [--out pwa/specs/catalog.json]

For every test type that pScheduler has installed, this builds a minimal
valid spec, then adds one option at a time and asks pScheduler which tools
could run the result (GET /tools?test=...).  pScheduler answers with each
tool's own "can-run" logic, so the table is what that version really does,
not what the documentation says.  It also records each test's description,
scheduling class, number of participants, and each tool's description and
preference order.

Test types the pScheduler does not have (optional bundles such as
"extras" and "snmp") are left out; pwa/specs/guidance.json covers them.

Run it against a perfSONAR host of the version you want to describe (the
psum05 testpoint, 5.2.6, for the committed file).  It only reads.
"""
import argparse
import datetime
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pwa import specforms  # noqa: E402

HOST, HOST2, ADDR, URL = "psum01.aglt2.org", "psum05.aglt2.org", "192.41.230.59", "https://www.aglt2.org/"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE        # perfSONAR hosts commonly use self-signed certificates


# Options whose support depends on the value chosen: every listed value is tried.
VALUE_PROBES = {
    ("latency", "protocol"): ["owamp", "twamp"],
    ("latencybg", "protocol"): ["owamp", "twamp"],
    ("rtt", "protocol"): ["icmp", "tcp", "twamp"],
    ("trace", "algorithm"): ["paris-traceroute"],
    ("trace", "probe-type"): ["icmp", "udp", "tcp"],
}


def get(base, path, **params):
    url = base.rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": "pwa-next-catalog"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_text": e.read().decode(errors="replace")[:300]}


def sample(name, node):
    """A plausible valid value for one option, or None if it cannot be guessed."""
    t = node.get("x-type")
    if "const" in node:
        return node["const"]
    if "enum" in node:
        choices = node["enum"]
        return 4 if 4 in choices else choices[0]
    if t in ("Host", "URLHostPort"):
        return HOST
    if t == "IPAddress":
        return ADDR
    if t in ("IPCIDR",):
        return "192.41.230.0/24"
    if t == "IPv6CIDR":
        return "2001:48a8:68f7:1::/64"
    if t == "URL" or name in ("url",):
        return URL
    if node.get("x-kind") == "duration":
        return "PT5S"
    typ = node.get("type")
    if typ == "boolean":
        return True
    if typ in ("integer", "number"):
        if "port" in name:
            return 5201
        lo, hi = node.get("minimum"), node.get("maximum")
        value = 1 if lo is None else max(lo, 1)
        if hi is not None and value > hi:
            value = hi
        if typ == "number" and hi is not None and hi <= 1:
            value = hi / 2
        return value
    if typ == "string":
        return {"query": "www.aglt2.org", "nameserver": "192.41.230.195", "record": "a",
                "host": HOST, "dest": HOST, "network": "192.41.230.0/24"}.get(name, "x")
    return None


def probe(base, test):
    doc, versions = specforms._versions("tests", test)
    last = versions[-1] or {}
    props = last.get("properties") or {}
    nodes = {n: specforms._resolve(doc, p) for n, p in props.items() if isinstance(p, dict)}
    spec = {}
    for name in last.get("required") or []:
        if name == "schema":
            continue
        value = sample(name, nodes.get(name, {}))
        if value is None:
            return {"error": "cannot build a minimal spec (required option %s)" % name}
        spec[name] = value
    if "source" in props and "source" not in spec:
        spec["source"] = HOST2

    def tools_for(s):
        s = dict(s, schema=specforms.schema_version("tests", test, s))
        res = get(base, "/tools", test=json.dumps({"type": test, "spec": s}))
        if isinstance(res, dict) and "_error" in res:
            return None, res["_text"]
        return [t["name"] for t in res], None

    base_tools, err = tools_for(spec)
    if base_tools is None:
        return {"error": "pScheduler rejected the minimal spec: %s" % err}
    parts = get(base, "/tests/%s/participants" % test,
                spec=json.dumps(dict(spec, schema=specforms.schema_version("tests", test, spec))))
    options, by_value, unchecked = {}, {}, []
    for f in specforms.fields("tests", test):
        name = f["name"]
        if name in spec or name in specforms.PARTICIPANT_FIELDS:
            continue
        values = VALUE_PROBES.get((test, name)) or [sample(name, nodes.get(name, {}))]
        if values == [None]:
            unchecked.append(name)
            continue
        results = {}
        for value in values:
            tools, err = tools_for(dict(spec, **{name: value}))
            if tools is not None:
                results[str(value)] = tools
        if not results:
            unchecked.append(name)
            continue
        options[name] = sorted({t for ts in results.values() for t in ts}, key=base_tools.index)
        if len(values) > 1:
            by_value[name] = results
    return {"base_tools": base_tools, "option_values": by_value,
            "participants": len(parts.get("participants") or []) if isinstance(parts, dict) else None,
            "options": options, "unchecked": sorted(unchecked)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pscheduler", default="https://localhost/pscheduler")
    ap.add_argument("--out", default=os.path.join(specforms.SPEC_DIR, "catalog.json"))
    a = ap.parse_args()
    tests = get(a.pscheduler, "/tests", expanded="true")
    tools = get(a.pscheduler, "/tools", expanded="true")
    if isinstance(tests, dict) or isinstance(tools, dict):
        sys.exit("cannot read %s: %s" % (a.pscheduler, tests if isinstance(tests, dict) else tools))
    catalog = {"_source": {"pscheduler": a.pscheduler, "built": datetime.date.today().isoformat(),
                           "note": "Built by tools/build_test_catalog.py; do not edit by hand."},
               "tools": {t["name"]: {"description": t.get("description"), "preference": t.get("preference"),
                                     "tests": t.get("tests")} for t in tools},
               "tests": {}}
    for t in sorted(tests, key=lambda t: t["name"]):
        name = t["name"]
        entry = {"description": t.get("description"), "scheduling_class": t.get("scheduling-class"),
                 "tools": sorted((x for x in catalog["tools"] if name in (catalog["tools"][x]["tests"] or [])),
                                 key=lambda x: -(catalog["tools"][x]["preference"] or 0))}
        if specforms.known("tests", name):
            entry.update(probe(a.pscheduler, name))
        catalog["tests"][name] = entry
        print("%-13s %-17s tools=%s %s" % (name, entry["scheduling_class"], ",".join(entry["tools"]),
                                           entry.get("error", "%d options checked" % len(entry.get("options", {})))))
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=1, sort_keys=True)
        f.write("\n")
    print("wrote", a.out)


if __name__ == "__main__":
    main()
