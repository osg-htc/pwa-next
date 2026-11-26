"""Tracking the follow-up with sites: who was contacted, when, how, and what came of it.

Each entry is a SiteContact row, kept by site name, newest first.  The latest
entry's status is the site's status on Reports > Follow-up with sites.
"""
import datetime
import re

from . import models as m
from .services import audit

STATUSES = {
    "asked": "Asked the site",
    "replied": "Site replied",
    "working": "Site is working on it",
    "fixed": "Site says it is fixed",
    "retired": "Site says the host is retired",
    "no-reply": "No reply yet",
}
VIAS = ("ticket", "e-mail", "meeting", "chat", "other")


def anchor(site):
    """The id of a site's card on the follow-up page."""
    return "site-" + re.sub(r"[^A-Za-z0-9_-]+", "-", site or "").strip("-")


def contacts_by_site(session):
    out = {}
    for c in session.query(m.SiteContact).order_by(m.SiteContact.contacted.desc(), m.SiteContact.id.desc()):
        out.setdefault(c.site, []).append(c)
    return out


def can_record(user, access, meshes):
    """Service administrators, and anyone who can change a mesh the site's silent hosts are in."""
    return bool(user and (user.is_admin or any(access.can("edit", "mesh", x) for x in meshes)))


def parse_date(text):
    if not text:
        return datetime.date.today()
    try:
        day = datetime.date.fromisoformat(str(text).strip())
    except ValueError:
        raise ValueError("The date needs the form YYYY-MM-DD.")
    if day > datetime.date.today():
        raise ValueError("The date is in the future.")
    return day


def record(session, actor, site, status, contacted=None, via=None, reference=None, note=None, hostname=None):
    """Add one entry.  Raises ValueError with a message for the person when something is wrong."""
    site = (site or "").strip()
    if not site:
        raise ValueError("Say which site.")
    if status not in STATUSES:
        raise ValueError("Status must be one of: %s." % ", ".join(STATUSES))
    if via and via not in VIAS:
        raise ValueError("How must be one of: %s." % ", ".join(VIAS))
    entry = m.SiteContact(site=site, hostname=(hostname or "").strip() or None, contacted=parse_date(contacted),
                          via=via or None, reference=(reference or "").strip()[:500] or None,
                          status=status, note=(note or "").strip(), created_by=getattr(actor, "label", actor))
    session.add(entry)
    audit(session, actor, "contact-site", object_type="site", label=site,
          detail={"status": status, "via": entry.via, "reference": entry.reference, "host": entry.hostname,
                  "date": entry.contacted.isoformat()})
    return entry


def delete(session, actor, entry):
    audit(session, actor, "delete-contact", object_type="site", label=entry.site,
          detail={"status": entry.status, "date": entry.contacted.isoformat(), "recorded_by": entry.created_by})
    session.delete(entry)
