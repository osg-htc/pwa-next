#!/usr/bin/env python3
"""Import a PWA export into a scratch database and compare what the new
publisher produces with what PWA publishes (see tools/fetch_baseline.py).

Usage: check_import.py <pwa-export-dir> <baseline-dir> [-v]
Exit status 0 when every file matches.
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pwa import compat, db, importer, models as m, publish   # noqa: E402


def main():
    export_dir, baseline = sys.argv[1], sys.argv[2]
    verbose = "-v" in sys.argv
    engine = db.make_engine("sqlite://")
    db.create_all(engine)
    session = db.make_session_factory(engine)()
    t0 = time.time()
    report = importer.import_pwa(session, export_dir)
    print(report)
    print("import took %.1fs" % (time.time() - t0))

    builder = publish.Builder(session, reference=False)   # PWA emitted no reference fields
    by_host = builder.tasks_by_host()
    same = different = 0
    for path in sorted(glob.glob(os.path.join(baseline, "config", "*.json"))):
        slug = os.path.basename(path)[:-5]
        mesh = session.query(m.Mesh).filter_by(slug=slug).one_or_none()
        if mesh is None:
            print("MISSING mesh", slug)
            different += 1
            continue
        diffs = compat.differences(json.load(open(path)), builder.mesh_template(mesh))
        same, different = report_one("config/" + slug, diffs, same, different, verbose)
    for path in sorted(glob.glob(os.path.join(baseline, "auto", "*.json"))):
        name = os.path.basename(path)[:-5]
        host = session.query(m.Host).filter_by(hostname=name).one_or_none()
        if host is None:
            print("MISSING host", name)
            different += 1
            continue
        diffs = compat.differences(json.load(open(path)),
                                   builder.host_template(host, by_host.get(host.id, [])))
        same, different = report_one("auto/" + name, diffs, same, different, verbose)
    print("same: %d   different: %d" % (same, different))
    return 1 if different else 0


def report_one(name, diffs, same, different, verbose):
    if not diffs:
        if verbose:
            print("ok   ", name)
        return same + 1, different
    print("DIFF ", name)
    for d in diffs:
        print("      " + d)
    return same, different + 1


if __name__ == "__main__":
    sys.exit(main())
