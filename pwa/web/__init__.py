"""The web application (Flask, as in pScheduler's API server)."""
import datetime
import os
import secrets

from flask import Flask, abort, g, redirect, request, session as cookie, url_for

from .. import authz, config, db
from .. import models as m
from .. import publish as publisher
from ..services import audit


def create_app(overrides=None):
    cfg = config.load(overrides)
    app = Flask(__name__)
    app.config.update(cfg)
    if not cfg["SECRET_KEY"] and cfg["AUTH"] != "dev" and not cfg.get("TESTING"):
        # A random key per process would log people out whenever Apache picks
        # another worker, so refuse to start without one.
        raise RuntimeError("PWA_SECRET_KEY is not set (see /etc/pwa/pwa.env)")
    app.secret_key = cfg["SECRET_KEY"] or secrets.token_hex(32)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    if cfg["DATABASE_URL"].startswith("sqlite:///"):
        os.makedirs(os.path.dirname(cfg["DATABASE_URL"][10:]) or ".", exist_ok=True)
    engine = db.make_engine(cfg["DATABASE_URL"])
    db.create_all(engine)
    Session = db.make_session_factory(engine)
    app.extensions["pwa_session"] = Session
    app.extensions["pwa_engine"] = engine

    @app.teardown_appcontext
    def _cleanup(exc):
        Session.remove()

    @app.before_request
    def _identify():
        g.db = Session()
        g.user = _current_user(app, g.db)
        g.access = authz.Access(g.db, g.user)
        if request.endpoint in ("static", "pub.config_index", "pub.config", "pub.auto",
                                "dev.login", "dev.login_as"):
            return None
        if g.user is None:
            if app.config["AUTH"] == "dev":
                return redirect(url_for("dev.login"))
            abort(401)
        if not g.user.active:
            abort(403, "This account has been disabled.")
        if request.method == "POST":
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, cookie.get("csrf", "")):
                abort(400, "Your session expired; reload the page and try again.")
        return None

    @app.context_processor
    def _helpers():
        if "csrf" not in cookie:
            cookie["csrf"] = secrets.token_hex(16)
        return {"user": g.get("user"), "access": g.get("access"), "csrf": cookie["csrf"],
                "site_name": app.config["SITE_NAME"], "auth_mode": app.config["AUTH"],
                "scopes": authz.SCOPES, "public_url": app.config["PUBLIC_URL"].rstrip("/")}

    @app.template_filter("ago")
    def _ago(when):
        if not when:
            return "never"
        s = (datetime.datetime.utcnow() - when).total_seconds()
        for unit, size in (("year", 31536000), ("month", 2592000), ("day", 86400),
                           ("hour", 3600), ("minute", 60)):
            if s >= size:
                n = int(s // size)
                return "%d %s%s ago" % (n, unit, "" if n == 1 else "s")
        return "just now"

    @app.template_filter("duration")
    def _duration(iso):
        from ..specforms import iso_to_seconds
        try:
            s = iso_to_seconds(iso)
        except (ValueError, TypeError):
            return iso or ""
        for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
            if s >= size and s % size == 0:
                n = int(s // size)
                return "%d %s%s" % (n, unit, "" if n == 1 else "s")
        return "%g seconds" % s

    from . import views
    for bp in views.blueprints:
        app.register_blueprint(bp)
    return app


def _current_user(app, session):
    """The signed-in person, created on first sight.  None if nobody is signed in."""
    cfg = app.config
    if cfg["AUTH"] == "dev":
        uid = cookie.get("uid")
        return session.get(m.User, uid) if uid else None
    env = request.environ
    subject = env.get(cfg["OIDC_SUBJECT"])
    if not subject:
        return None
    name, email, idp = (env.get(cfg[k]) for k in ("OIDC_NAME", "OIDC_EMAIL", "OIDC_IDP"))
    user = session.query(m.User).filter_by(subject=subject).one_or_none()
    if user is None:
        alias = session.get(m.UserIdentity, subject)
        user = alias.user if alias else None
    if user is None:
        user = m.User(subject=subject, name=name, email=email, idp=idp,
                      is_admin=subject in cfg["ADMINS"] or (email or "") in cfg["ADMINS"])
        session.add(user)
        audit(session, user, "first-login", object_type="user", label=name or email or subject,
              detail={"idp": idp, "admin": user.is_admin})
    else:
        user.name, user.email, user.idp = name or user.name, email or user.email, idp or user.idp
    now = datetime.datetime.utcnow()
    if not user.last_login or (now - user.last_login).total_seconds() > 300:
        user.last_login = now
    session.commit()
    return user


def publish_now(app, session):
    """Re-render and publish after a change.  Returns the publisher's result."""
    return publisher.publish_all(session, app.config["PUBLISH_DIR"], app.config["PUBLIC_URL"])
