#!/usr/bin/env python3
"""Fetch what a running PWA publishes, as the reference for the importer test.

Usage: fetch_baseline.py <pwa-export-dir> <out-dir> --base https://HOST/pub [--auto N]

Reads configs.json / hostgroups.json / hosts.json from a `mongoexport` of the
PWA database (see docs/MIGRATION.md) to learn which configs and hosts exist,
then fetches /pub/config/<url> for every config and /pub/auto/<host> for a
deterministic sample of N hosts that belong to at least one host group.

Requests are sequential on purpose: PWA 5.x mixes hosts between meshes when
it serves requests in parallel (psconfig-web issue #214).
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pwa-next-baseline"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--base", required=True, help="PWA publisher URL, e.g. https://HOST/pub")
    ap.add_argument("--auto", type=int, default=40)
    ap.add_argument("--always", default="", help="comma-separated hostname suffixes always included")
    a = ap.parse_args()

    load = lambda n: json.load(open(os.path.join(a.export_dir, n + ".json"), encoding="utf-8"))
    configs, groups, hosts = load("configs"), load("hostgroups"), load("hosts")
    by_id = {h["_id"]["$oid"]: h["hostname"] for h in hosts}
    members = sorted({by_id[m["$oid"]] for g in groups for m in g.get("hosts", []) if m["$oid"] in by_id})
    # deterministic sample: order by hash of the name
    ranked = sorted(members, key=lambda n: hashlib.sha1(n.encode()).hexdigest())
    always = [s for s in a.always.split(",") if s]
    sample = sorted(set(ranked[: a.auto]) | {m for m in members if any(m.endswith(s) for s in always)})

    os.makedirs(os.path.join(a.out_dir, "config"), exist_ok=True)
    os.makedirs(os.path.join(a.out_dir, "auto"), exist_ok=True)
    failed = 0
    jobs = [("config", c["url"]) for c in configs] + [("auto", h) for h in sample]
    for kind, name in jobs:
        url = "%s/%s/%s?format=psconfig" % (a.base, kind, urllib.parse.quote(name))
        dest = os.path.join(a.out_dir, kind, name + ".json")
        try:
            body = fetch(url)
            json.loads(body)
            with open(dest, "wb") as f:
                f.write(body)
        except (urllib.error.URLError, ValueError, OSError) as e:
            failed += 1
            print("FAILED %s: %s" % (url, e), file=sys.stderr)
    print("fetched %d, failed %d -> %s" % (len(jobs) - failed, failed, a.out_dir))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
