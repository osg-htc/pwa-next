"""Site branding: name, tagline, logo, colours, about text and custom links.

Stored in the `settings` table so a service administrator can change it from
the Branding page without a redeploy.  Everything is validated on the way in:
colours must be #rrggbb, links must be http(s) or site-relative, and the logo
must really be a PNG, JPEG, GIF, WebP or SVG image of modest size.
"""
import base64
import re

from . import models as m

KEY = "branding"
MAX_LOGO_BYTES = 512 * 1024
MAX_LINKS = 10
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULTS = {
    "site_name": None,            # None = PWA_SITE_NAME from the configuration
    "tagline": "",
    "about": "",                  # plain text; blank lines separate paragraphs; URLs become links
    "links": [],                  # [{"label": ..., "url": ...}]
    "accent": "#1565c0",          # pwa-next logo blue
    "header": "#0b2a4a",          # pwa-next logo navy
    "contact": "",                # e-mail address or URL for questions
}

_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def get(session, default_name="pSConfig Web Admin"):
    row = session.get(m.Setting, KEY)
    value = dict(DEFAULTS, **((row.value if row else None) or {}))
    value["site_name"] = value.get("site_name") or default_name
    value["has_logo"] = bool(session.get(m.Setting, KEY + ".logo"))
    return value


def logo(session):
    """(mimetype, bytes) or None."""
    row = session.get(m.Setting, KEY + ".logo")
    if not row or not row.value:
        return None
    return row.value["type"], base64.b64decode(row.value["data"])


def _link_ok(url):
    return bool(re.match(r"^(https?://[^\s<>\"]+|/[^\s<>\"]*)$", url or ""))


def validate(form):
    """Return (settings, errors) from submitted form fields."""
    errors = []
    value = {
        "site_name": (form.get("site_name") or "").strip()[:80] or None,
        "tagline": (form.get("tagline") or "").strip()[:160],
        "about": (form.get("about") or "").strip()[:4000],
        "contact": (form.get("contact") or "").strip()[:200],
        "accent": (form.get("accent") or DEFAULTS["accent"]).strip(),
        "header": (form.get("header") or DEFAULTS["header"]).strip(),
        "links": [],
    }
    for name in ("accent", "header"):
        if not HEX.match(value[name]):
            errors.append("Colours must look like #1a2b3c.")
            value[name] = DEFAULTS[name]
    contact = value["contact"]
    if contact and not (re.match(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$", contact) or _link_ok(contact)):
        errors.append("Contact must be an e-mail address or a web address.")
    for label, url in zip(form.getlist("link_label"), form.getlist("link_url")):
        label, url = label.strip()[:60], url.strip()[:500]
        if not label and not url:
            continue
        if not label or not _link_ok(url):
            errors.append("Each link needs a name and a web address starting with https://, http:// or /.")
            continue
        value["links"].append({"label": label, "url": url})
    if len(value["links"]) > MAX_LINKS:
        errors.append("At most %d links." % MAX_LINKS)
        value["links"] = value["links"][:MAX_LINKS]
    return value, errors


def check_logo(data):
    """Mimetype of an uploaded logo, or raise ValueError."""
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError("The logo must be smaller than %d KB." % (MAX_LOGO_BYTES // 1024))
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:1024].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower()):
        return "image/svg+xml"
    raise ValueError("The logo must be a PNG, JPEG, GIF, WebP or SVG image.")


def save(session, value):
    row = session.get(m.Setting, KEY)
    if row is None:
        session.add(m.Setting(key=KEY, value=value))
    else:
        row.value = value


def save_logo(session, mime, data):
    row = session.get(m.Setting, KEY + ".logo")
    value = {"type": mime, "data": base64.b64encode(data).decode()}
    if row is None:
        session.add(m.Setting(key=KEY + ".logo", value=value))
    else:
        row.value = value


def remove_logo(session):
    row = session.get(m.Setting, KEY + ".logo")
    if row is not None:
        session.delete(row)


def paragraphs(text):
    """About text as a list of paragraphs, each a list of (kind, text) pieces."""
    out = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip()
        if not para:
            continue
        pieces, pos = [], 0
        for match in re.finditer(r"https?://[^\s<>\"]+[^\s<>\".,;:)]", para):
            if match.start() > pos:
                pieces.append(("text", para[pos:match.start()]))
            pieces.append(("link", match.group(0)))
            pos = match.end()
        if pos < len(para):
            pieces.append(("text", para[pos:]))
        out.append(pieces)
    return out
