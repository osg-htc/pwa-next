#!/usr/bin/env python3
"""Write users.json for `pwa import-pwa` from PWA's auth service database.

Usage: export_auth_users.py <auth.sqlite> <export-dir>

Reads only names, e-mail addresses, federated identities and the
administrator flag.  Password hashes and tokens are never read.  The database
is opened read-only.  The output names real people: keep it private.
"""
import json
import os
import sqlite3
import sys


def main():
    source, out_dir = sys.argv[1], sys.argv[2]
    con = sqlite3.connect("file:%s?mode=ro" % source, uri=True)
    rows = con.execute("select id, fullname, username, email, oidc_subs, scopes, active from Users order by id")
    users = []
    for ident, fullname, username, email, oidc_subs, scopes, active in rows:
        try:
            identities = json.loads(oidc_subs) if oidc_subs else []
        except ValueError:
            identities = []
        subs, idp = [], None
        for item in identities if isinstance(identities, list) else []:
            sub = item.get("sub") if isinstance(item, dict) else item
            if sub and sub not in subs:
                subs.append(sub)
                idp = idp or (item.get("idp_name") if isinstance(item, dict) else None)
        try:
            scope = json.loads(scopes) if scopes else {}
        except ValueError:
            scope = {}
        admin = any("admin" in (scope.get(k) or []) for k in ("pwa", "mca")) if isinstance(scope, dict) else False
        users.append({"id": ident, "fullname": fullname or username, "email": email, "subs": subs,
                      "idp": idp, "admin": admin, "active": bool(active)})
    path = os.path.join(out_dir, "users.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(users, f, indent=1)
    print("%d accounts written to %s (%d with a federated identity, %d administrators)" % (
        len(users), path, sum(1 for u in users if u["subs"]), sum(1 for u in users if u["admin"])))


if __name__ == "__main__":
    main()
