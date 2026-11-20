"""Command line: python -m pwa <command>

  init-db                    create the tables
  import-pwa <export-dir>    load a pSConfig Web Admin export into an empty database
  publish                    render and publish everything
  lookup-sync [--dry-run]    refresh the host inventory from the Lookup Service, then publish
  serve [--port N]           development web server (never for production)
"""
import argparse
import sys

from . import config, db, importer, publish


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pwa", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    p = sub.add_parser("import-pwa")
    p.add_argument("export_dir")
    sub.add_parser("publish")
    p = sub.add_parser("lookup-sync")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--url")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8085)
    p.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    if args.command == "serve":
        from .web import create_app
        create_app().run(host=args.host, port=args.port, debug=False)
        return 0

    cfg = config.load()
    engine = db.make_engine(cfg["DATABASE_URL"])
    db.create_all(engine)
    session = db.make_session_factory(engine)()
    if args.command == "init-db":
        print("tables ready in", cfg["DATABASE_URL"].split("@")[-1])
    elif args.command == "import-pwa":
        from . import models as m
        if session.query(m.Mesh).count() or session.query(m.Host).count():
            print("refusing to import: the database already has hosts or meshes", file=sys.stderr)
            return 1
        print(importer.import_pwa(session, args.export_dir))
    elif args.command == "lookup-sync":
        from . import lookup
        services, hosts = lookup.fetch(args.url or cfg["LOOKUP_URL"])
        r = lookup.sync(session, services, hosts, dry_run=args.dry_run)
        print("%sLookup Service lists %d hosts: %d new, %d updated, %d unchanged, %d now self-registering" % (
            "(dry run) " if args.dry_run else "", r["registered"], len(r["created"]), r["updated"],
            r["unchanged"], len(r["adopted"])))
        if args.dry_run:
            for name in r["created"][:40]:
                print("  new:", name)
            return 0
    if args.command in ("import-pwa", "publish", "lookup-sync"):
        r = publish.publish_all(session, cfg["PUBLISH_DIR"], cfg["PUBLIC_URL"])
        print("published to %s: %d written, %d unchanged, %d removed" % (
            cfg["PUBLISH_DIR"], r["written"], r["unchanged"], r["removed"]))
        for path, reason in r["failed"]:
            print("NOT PUBLISHED %s: %s" % (path, reason))
        for slug, task, reason in r["withheld"]:
            print("withheld task %r in mesh %s: %s" % (task, slug, reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
