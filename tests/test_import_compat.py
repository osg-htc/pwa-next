"""Optional: check the importer against a real PWA.

Set PWA_EXPORT_DIR to a `mongoexport` of the PWA database and
PWA_BASELINE_DIR to what that PWA published at the same moment
(tools/fetch_baseline.py).  Skipped otherwise, since the data is site-specific.
"""
import glob
import json
import os

import pytest

from pwa import compat, db as dbmod, importer, models as m, publish

EXPORT, BASELINE = os.environ.get("PWA_EXPORT_DIR"), os.environ.get("PWA_BASELINE_DIR")
pytestmark = pytest.mark.skipif(not (EXPORT and BASELINE), reason="needs PWA_EXPORT_DIR and PWA_BASELINE_DIR")


def test_imported_data_publishes_what_pwa_published():
    engine = dbmod.make_engine("sqlite://")
    dbmod.create_all(engine)
    session = dbmod.make_session_factory(engine)()
    importer.import_pwa(session, EXPORT)
    builder = publish.Builder(session, reference=False)        # PWA emitted no reference fields
    by_host = builder.tasks_by_host()
    checked, problems = 0, []
    for path in sorted(glob.glob(os.path.join(BASELINE, "config", "*.json"))):
        mesh = session.query(m.Mesh).filter_by(slug=os.path.basename(path)[:-5]).one()
        diffs = compat.differences(json.load(open(path)), builder.mesh_template(mesh))
        checked += 1
        problems += ["%s: %s" % (mesh.slug, d) for d in diffs]
    for path in sorted(glob.glob(os.path.join(BASELINE, "auto", "*.json"))):
        host = session.query(m.Host).filter_by(hostname=os.path.basename(path)[:-5]).one()
        diffs = compat.differences(json.load(open(path)), builder.host_template(host, by_host.get(host.id, [])))
        checked += 1
        problems += ["%s: %s" % (host.hostname, d) for d in diffs]
    assert checked > 0 and problems == []
