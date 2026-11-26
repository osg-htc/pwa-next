"""Who runs each perfSONAR host, and how to reach them.

`pwa site-sync` reads three public registries:

- WLCG CRIC: the perfSONAR services each WLCG site has registered (site,
  federation, tier, experiments, and whether CRIC still lists it as production);
- GOCDB: the site list (NGI, country, the portal page where people with access
  see the site's contacts);
- OSG Topology: OSG resources, with the names and roles of their contacts.

None of them gives e-mail addresses without credentials. What they do give is
the route: WLCG sites are reached by a ticket to the site (GGUS), which reaches
the contacts GOCDB holds; OSG sites by their Topology contacts.

The result is kept in one settings row, keyed by host name, so the hourly
Lookup Service refresh (which replaces each host's Lookup Service data) leaves
it alone and no table changes.
"""
import datetime
import json
import os
import ssl
import urllib.request
import xml.etree.ElementTree as ET

from . import models as m

CRIC_SERVICES = "https://wlcg-cric.cern.ch/api/core/service/query/?json&type=PerfSonar"
CRIC_SITES = "https://wlcg-cric.cern.ch/api/core/rcsite/query/?json"
GOCDB_SITES = "https://goc.egi.eu/gocdbpi/public/?method=get_site_list"
OSG_TOPOLOGY = "https://topology.opensciencegrid.org/rgsummary/xml"
GOCDB_PORTAL = "https://goc.egi.eu/portal/index.php?Page_Type=Site&id=%s"
SETTING_KEY = "site_registry"
# second-level labels under which a domain name has three parts (ph.ed.ac.uk -> ed.ac.uk)
_SLD = {"ac", "co", "com", "edu", "gov", "net", "org", "or", "go", "res", "sci"}


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _get(url, ca=None, timeout=180):
    """GET a URL.  `ca` adds a CA file or directory (CRIC's chain ends at CERN
    Root CA 2, which is in the IGTF bundle but not in RHEL's default trust)."""
    ctx = ssl.create_default_context()
    if ca:
        ctx.load_verify_locations(capath=ca) if os.path.isdir(ca) else ctx.load_verify_locations(cafile=ca)
    req = urllib.request.Request(url, headers={"User-Agent": "pwa-next site-sync"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


# --------------------------------------------------------------------------
# parsing (each takes the raw bytes a registry returns)
# --------------------------------------------------------------------------

def parse_cric_services(data):
    """{endpoint (lower case): [service records]}"""
    out = {}
    for rec in json.loads(data).values():
        ep = (rec.get("endpoint") or "").strip().lower()
        if ep:
            out.setdefault(ep, []).append(rec)
    return out


def parse_cric_sites(data):
    out = {}
    for name, rec in json.loads(data).items():
        out[name] = {"tier": rec.get("rc_tier_level"), "country": rec.get("country"),
                     "federations": [f.get("name") if isinstance(f, dict) else f for f in rec.get("federations") or []],
                     "osg": bool(rec.get("oim_groupid")), "state": rec.get("state"), "url": rec.get("infourl") or None}
    return out


def parse_gocdb(data):
    out = {}
    for site in ET.fromstring(data).iter("SITE"):
        a = site.attrib
        if a.get("NAME"):
            out[a["NAME"]] = {"id": a.get("ID"), "ngi": a.get("ROC"), "country": a.get("COUNTRY")}
    return out


def parse_osg(data):
    """{fqdn (lower case): resource} for every OSG resource."""
    out = {}
    for rg in ET.fromstring(data).iter("ResourceGroup"):
        group, site = rg.findtext("GroupName"), rg.findtext("Site/Name")
        facility = rg.findtext("Facility/Name")
        for res in rg.iter("Resource"):
            fqdn = (res.findtext("FQDN") or "").strip().lower()
            if not fqdn:
                continue
            contacts = {}
            for cl in res.iter("ContactList"):
                kind = cl.findtext("ContactType") or "Contact"
                names = [c.findtext("Name") for c in cl.iter("Contact") if c.findtext("Name")]
                if names:
                    contacts.setdefault(kind, []).extend(names)
            out[fqdn] = {"site": site, "facility": facility, "resource_group": group,
                         "resource": res.findtext("Name"),
                         "services": [s.findtext("Name") for s in res.iter("Service") if s.findtext("Name")],
                         "contacts": contacts}
    return out


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def domain_of(hostname):
    parts = hostname.lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def build(hosts, cric_services, cric_sites, gocdb, osg):
    """{hostname: what the registries say about it}; hosts they do not know are left out.

    `hosts` = [(hostname, site name the host reports to the Lookup Service)].  A host is
    matched by its own CRIC entry first, then by a reported site name that is a WLCG
    site's name, then by its own OSG Topology entry, and only then by a domain that
    belongs to exactly one CRIC site (a guess, labelled as such).
    """
    by_domain = {}
    for ep, recs in cric_services.items():
        by_domain.setdefault(domain_of(ep), set()).add(recs[0].get("rcsite"))
    out = {}
    for name, ls_site in hosts:
        key = name.lower()
        recs, entry, site = cric_services.get(key), {}, None
        if recs:
            entry["matched"] = "CRIC"
            site = recs[0].get("rcsite")
        elif ls_site and (ls_site in cric_sites or ls_site in gocdb):
            entry["matched"] = "site name it reports"
            site = ls_site
        elif key not in osg:                                # a guess, so only when nothing exact matched
            sites = by_domain.get(domain_of(key)) or set()
            site = next(iter(sites)) if len(sites) == 1 else None
            if site:
                entry["matched"] = "CRIC site with the same domain"
        if site:
            info = cric_sites.get(site) or {}
            entry["cric"] = {"site": site, "tier": info.get("tier"), "country": info.get("country"),
                             "federation": (recs[0].get("federation") if recs else None)
                             or ", ".join(x for x in info.get("federations") or [] if x) or None,
                             "experiments": sorted({vo for r in recs or [] for vo in (r.get("usage") or {})}),
                             "services": [{"flavour": r.get("flavour"), "state": r.get("state"),
                                           "status": r.get("status")} for r in recs or []]}
            if site in gocdb:
                g = gocdb[site]
                entry["gocdb"] = {"site": site, "ngi": g.get("ngi"), "country": g.get("country"),
                                  "portal": GOCDB_PORTAL % g["id"] if g.get("id") else None}
        if key in osg:
            entry["osg"] = osg[key]
            entry.setdefault("matched", "OSG Topology")
        if entry:
            entry["site"] = (entry.get("cric") or {}).get("site") or (entry.get("osg") or {}).get("site")
            entry["reach"] = reach(entry)
            out[name] = entry
    return out


def reach(entry):
    """How to get a message to the people who run the host, best first."""
    ways = []
    if entry.get("gocdb"):
        ways.append("Ticket to WLCG site %s (GGUS); contacts in GOCDB" % entry["gocdb"]["site"])
    elif entry.get("cric"):
        ways.append("Ticket to WLCG site %s (GGUS)" % entry["cric"]["site"])
    osg = entry.get("osg")
    if osg and osg.get("contacts"):
        for kind in ("Administrative Contact", "Site Contact", "Local Operational Contact"):
            if osg["contacts"].get(kind):
                ways.append("OSG Topology %s: %s" % (kind.lower(), ", ".join(osg["contacts"][kind][:3])))
                break
        else:
            kind, names = next(iter(osg["contacts"].items()))
            ways.append("OSG Topology %s: %s" % (kind.lower(), ", ".join(names[:3])))
    return ways


# --------------------------------------------------------------------------
# sync and lookup
# --------------------------------------------------------------------------

def sync(session, ca=None, dry_run=False, fetch=_get):
    """Fetch the registries, match every inventory host, store the result."""
    sources, errors = {}, {}
    for key, url, parse in (("cric_services", CRIC_SERVICES, parse_cric_services),
                            ("cric_sites", CRIC_SITES, parse_cric_sites),
                            ("gocdb", GOCDB_SITES, parse_gocdb), ("osg", OSG_TOPOLOGY, parse_osg)):
        try:
            sources[key] = parse(fetch(url, ca))
        except Exception as e:                      # one registry down must not stop the rest
            sources[key], errors[key] = {}, "%s: %s" % (type(e).__name__, e)
    names = [(h, site) for h, site in session.query(m.Host.hostname, m.Host.site)]
    hosts = build(names, sources["cric_services"], sources["cric_sites"], sources["gocdb"], sources["osg"])
    value = {"fetched": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
             "counts": {k: len(v) for k, v in sources.items()}, "errors": errors, "hosts": hosts}
    if not dry_run:
        row = session.get(m.Setting, SETTING_KEY) or m.Setting(key=SETTING_KEY)
        row.value = value
        session.add(row)
        session.commit()
    return value


def registry(session):
    row = session.get(m.Setting, SETTING_KEY)
    return (row.value if row else None) or {"fetched": None, "hosts": {}, "errors": {}, "counts": {}}
