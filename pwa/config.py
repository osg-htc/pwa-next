"""Settings, read from the environment (or a file named by PWA_CONFIG).

PWA_DATABASE_URL   SQLAlchemy URL.  PostgreSQL in production.
PWA_PUBLISH_DIR    Where the published files are written (served by Apache).
PWA_PUBLIC_URL     Public URL of that directory, e.g. https://host/pub
PWA_AUTH           "oidc"  identity comes from Apache: whoever it authenticated
                           (REMOTE_USER).  Meant for mod_auth_openidc, but any
                           Apache authentication module works.
                   "dev"   pick a user on a local page; never use in production
PWA_ADMINS         Comma-separated subjects or e-mail addresses made service
                   administrators at their first login.
PWA_SECRET_KEY     Signs the browser session cookie.
PWA_SECRET_KEY_FILE  Alternatively, a file holding that key (keeps it out of
                   configuration management).
PWA_LOOKUP_URL     Lookup Service search endpoint for `pwa lookup-sync`.
PWA_REGISTRY_CA    CA file or directory added for `pwa site-sync` (WLCG CRIC's chain ends at
                   CERN Root CA 2: /etc/grid-security/certificates has it).
PWA_MERGE_DUPLICATES  Test types (comma-separated, e.g. "trace") whose identical tasks in
                   several meshes measure each shared host pair once, in the per-host feeds.
                   Empty (the default) publishes every task as defined.
PWA_ISSUES_URL     Where the Help page sends bug reports and feature requests
                   (default: the pwa-next issue tracker on GitHub).
"""
import os


def _read(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read().strip() or None


def load(overrides=None):
    path = os.environ.get("PWA_CONFIG")
    values = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip('"').strip("'")
    get = lambda k, d=None: os.environ.get(k, values.get(k, d))
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cfg = {
        "DATABASE_URL": get("PWA_DATABASE_URL", "sqlite:///" + os.path.join(here, "var", "pwa.db")),
        "PUBLISH_DIR": get("PWA_PUBLISH_DIR", os.path.join(here, "var", "pub")),
        "PUBLIC_URL": get("PWA_PUBLIC_URL", "http://127.0.0.1:8085/pub"),
        "AUTH": get("PWA_AUTH", "oidc"),
        "ADMINS": [x.strip() for x in (get("PWA_ADMINS", "") or "").split(",") if x.strip()],
        "SECRET_KEY": get("PWA_SECRET_KEY") or _read(get("PWA_SECRET_KEY_FILE")),
        # WSGI variables mod_auth_openidc sets (OIDCRemoteUserClaim sub).
        "OIDC_SUBJECT": get("PWA_OIDC_SUBJECT", "REMOTE_USER"),
        "OIDC_NAME": get("PWA_OIDC_NAME", "OIDC_CLAIM_name"),
        "OIDC_EMAIL": get("PWA_OIDC_EMAIL", "OIDC_CLAIM_email"),
        "OIDC_IDP": get("PWA_OIDC_IDP", "OIDC_CLAIM_idp_name"),
        "SITE_NAME": get("PWA_SITE_NAME", "pSConfig Web Admin"),
        "LOOKUP_URL": get("PWA_LOOKUP_URL", "http://35.223.142.206/lookup/_search"),
        "ISSUES_URL": get("PWA_ISSUES_URL", "https://github.com/osg-htc/pwa-next/issues"),
        "REGISTRY_CA": get("PWA_REGISTRY_CA"),
        "MERGE_DUPLICATES": tuple(x.strip() for x in (get("PWA_MERGE_DUPLICATES", "") or "").split(",") if x.strip()),
    }
    cfg.update(overrides or {})
    return cfg
