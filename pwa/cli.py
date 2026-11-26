"""Command line: python -m pwa <command>

  init-db                    create the tables
  import-pwa <export-dir>    load a pSConfig Web Admin export into an empty database
  publish                    render and publish everything
  lookup-sync [--dry-run]    refresh the host inventory from the Lookup Service, then publish
  branding show              print the current branding as JSON
  branding set FILE [--logo IMAGE]
                             set branding from a JSON file (and optionally the logo),
                             as the Branding page would; for scripted deployments
  serve [--port N]           development web server (never for production)
  site-sync [--dry-run]      record who runs each host and how to reach them, from
                             WLCG CRIC, GOCDB and OSG Topology (Reports > follow-up)
  mcp --as LOGIN [--allow-changes]
                             MCP server on standard input/output for AI assistants,
                             acting as that account (read-only unless --allow-changes);
                             see docs/MCP.md
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
    p = sub.add_parser("branding")
    p.add_argument("action", choices=["show", "set"])
    p.add_argument("file", nargs="?")
    p.add_argument("--logo")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8085)
    p.add_argument("--host", default="127.0.0.1")
    p = sub.add_parser("site-sync")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("mcp")
    p.add_argument("--as", dest="login", required=True,
                   help="the account to act as: its login (CILogon subject or password-file name) or e-mail")
    p.add_argument("--allow-changes", action="store_true",
                   help="also offer the tools that change things (within the account's permissions)")
    args = ap.parse_args(argv)

    if args.command == "serve":
        from .web import create_app
        create_app().run(host=args.host, port=args.port, debug=False)
        return 0

    if args.command == "mcp":
        from . import mcp
        return mcp.main(config.load(), args.login, args.allow_changes)

    cfg = config.load()
    engine = db.make_engine(cfg["DATABASE_URL"])
    before, after = db.create_all(engine)
    session = db.make_session_factory(engine)()
    if args.command == "init-db":
        print("tables ready in %s (schema revision %s%s)" % (
            cfg["DATABASE_URL"].split("@")[-1], after, "" if before == after else ", was %s" % (before or "none")))
    elif args.command == "import-pwa":
        from . import models as m
        if session.query(m.Mesh).count() or session.query(m.Host).count():
            print("refusing to import: the database already has hosts or meshes", file=sys.stderr)
            return 1
        print(importer.import_pwa(session, args.export_dir))
    elif args.command == "branding":
        return _branding(session, cfg, args)
    elif args.command == "site-sync":
        from . import sites
        r = sites.sync(session, ca=cfg.get("REGISTRY_CA"), dry_run=args.dry_run)
        hosts = r["hosts"]
        import collections
        how = collections.Counter(v.get("matched") for v in hosts.values())
        print("%sregistries: %s; %d inventory hosts matched (%s)" % (
            "(dry run) " if args.dry_run else "",
            ", ".join("%s %d" % (k, n) for k, n in r["counts"].items()), len(hosts),
            ", ".join("%d by %s" % (n, k) for k, n in how.most_common())))
        for k, e in r["errors"].items():
            print("could not read %s: %s" % (k, e), file=sys.stderr)
        return 1 if r["errors"] else 0
    elif args.command == "lookup-sync":
        from . import lookup
        services, hosts = lookup.fetch(args.url or cfg["LOOKUP_URL"])
        if cfg.get("LOOKUP_NEW_URL"):
            new_services, new_hosts = lookup.fetch_new(cfg["LOOKUP_NEW_URL"])
            print("next-generation index: %d hosts" % len(new_hosts))
            services, hosts = new_services + services, new_hosts + hosts   # current format last: it wins
        r = lookup.sync(session, services, hosts, dry_run=args.dry_run)
        print("%sLookup Service lists %d hosts: %d new, %d updated, %d unchanged, %d now self-registering" % (
            "(dry run) " if args.dry_run else "", r["registered"], len(r["created"]), r["updated"],
            r["unchanged"], len(r["adopted"])))
        if args.dry_run:
            for name in r["created"][:40]:
                print("  new:", name)
            return 0
    if args.command in ("import-pwa", "publish", "lookup-sync"):
        r = publish.publish_all(session, cfg["PUBLISH_DIR"], cfg["PUBLIC_URL"], merge=cfg["MERGE_DUPLICATES"])
        print("published to %s: %d written, %d unchanged, %d removed" % (
            cfg["PUBLISH_DIR"], r["written"], r["unchanged"], r["removed"]))
        for path, reason in r["failed"]:
            print("NOT PUBLISHED %s: %s" % (path, reason))
        for slug, task, reason in r["withheld"]:
            print("withheld task %r in mesh %s: %s" % (task, slug, reason))
    return 0


def _branding(session, cfg, args):
    import json
    from . import branding, models
    from .services import audit
    if args.action == "show":
        value = branding.get(session, cfg["SITE_NAME"])
        print(json.dumps({k: v for k, v in value.items() if k != "has_logo"}, indent=2, ensure_ascii=False))
        print("logo: %s" % ("set" if value["has_logo"] else "none (the pwa-next icon is shown)"), file=sys.stderr)
        return 0
    if not args.file:
        print("branding set needs a JSON file", file=sys.stderr)
        return 2
    with open(args.file, encoding="utf-8") as f:
        wanted = json.load(f)

    class Form(dict):                      # what branding.validate expects from a web form
        def getlist(self, key):
            field = {"link_label": "label", "link_url": "url"}[key]
            return [str(link.get(field, "")) for link in wanted.get("links") or []]
    value, errors = branding.validate(Form({k: v for k, v in wanted.items() if k != "links" and v is not None}))
    if args.logo:
        with open(args.logo, "rb") as f:
            data = f.read()
        try:
            branding.save_logo(session, branding.check_logo(data), data)
        except ValueError as e:
            errors.append(str(e))
    if errors:
        session.rollback()
        for e in errors:
            print("not set: " + e, file=sys.stderr)
        return 1
    branding.save(session, value)
    audit(session, "pwa branding set", "branding", object_type="system", label="branding",
          detail={"file": args.file, "logo": bool(args.logo)})
    session.commit()
    print("branding set: %s" % value["site_name"] if value.get("site_name") else "branding set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
