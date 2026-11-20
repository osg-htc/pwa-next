"""Keep the host inventory in step with the perfSONAR Lookup Service.

Two queries against the Lookup Service's search endpoint (every live
pScheduler service, every live host record) are joined in memory, and each
host is created or refreshed by name.  A host that stops registering is not
deleted: its `last_seen` simply stops moving, and the UI flags it.

This is the only module that knows the Lookup Service's record format, which
is due to change with the next perfSONAR release.
"""
import concurrent.futures
import datetime
import ipaddress
import json
import socket
import urllib.parse
import urllib.request

from . import models as m

DEFAULT_URL = "http://35.223.142.206/lookup/_search"    # what perfSONAR 5.2.6 itself uses
INFO_PREFIXES = ("host-", "pshost-", "location-")
INFO_SKIP = {"host-administrators", "host-net-interfaces", "host-name"}


def _query(url, must, timeout=60):
    body = json.dumps({"query": {"bool": {"must": must,
                                          "filter": [{"range": {"expires": {"gt": "now"}}}]}},
                       "size": 10000}).encode()
    req = urllib.request.Request(url, data=body, method="GET",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "psconfig-web-admin"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return [hit["_source"] for hit in json.load(resp)["hits"]["hits"]]


def fetch(url=DEFAULT_URL):
    """Return (pScheduler service records, host records) currently registered."""
    services = _query(url, [{"term": {"type": "service"}},
                            {"term": {"service-type.keyword": "pscheduler"}}])
    hosts = _query(url, [{"term": {"type": "host"}}])
    return services, hosts


def _first(record, key):
    v = record.get(key)
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _is_ip(text):
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


def candidate_names(service, host, ptr=None):
    """Names a registration could go by, best first.

    1. where the host says its pScheduler answers (right for multi-homed
       hosts, which register one service per interface);
    2. reverse DNS of the address it registered (how PWA named hosts);
    3. any host name it listed; 4. the bare address, as a last resort.
    """
    ptr = ptr or {}
    locators = [urllib.parse.urlsplit(l).hostname for l in service.get("service-locator") or []]
    listed = [n for n in (host or {}).get("host-name") or [] if n]
    out = [n for n in locators if n and not _is_ip(n)]
    out += [ptr[n] for n in listed + [l for l in locators if l] if _is_ip(n) and n in ptr]
    out += [n for n in listed if not _is_ip(n)]
    out += [n for n in locators + listed if n and _is_ip(n)]
    seen, unique = set(), []
    for n in out:
        n = n.lower().strip("[]")
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def addresses_of(service, host):
    locators = [urllib.parse.urlsplit(l).hostname for l in service.get("service-locator") or []]
    return [n for n in ((host or {}).get("host-name") or []) + locators if n and _is_ip(n)]


def reverse_dns(addresses, workers=48):
    """{ip: name} for the addresses that have a PTR record."""
    def one(ip):
        try:
            return ip, socket.gethostbyaddr(ip.strip("[]"))[0].lower().rstrip(".")
        except (OSError, UnicodeError):
            return ip, None
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return {ip: name for ip, name in pool.map(one, sorted(set(addresses))) if name}


def describe(service, host):
    """Inventory fields for one registered pScheduler service."""
    host = host or {}
    addresses = []
    for n in host.get("host-name") or []:
        if n and _is_ip(n):
            addresses.append({"address": n, "family": ipaddress.ip_address(n).version})
    info = {}
    for key, value in host.items():
        if key in INFO_SKIP or not key.startswith(INFO_PREFIXES):
            continue
        v = _first(host, key)
        if v not in (None, ""):
            info[key] = v
    return {"site": _first(host, "location-sitename"),
            "communities": sorted(set((host.get("group-communities") or []) +
                                      (service.get("group-communities") or []))),
            "capabilities": sorted(set(service.get("pscheduler-tests") or [])),
            "addresses": addresses, "info": info}


def sync(session, services, hosts, now=None, dry_run=False, resolve=reverse_dns):
    """Create or refresh inventory hosts.  Returns a summary dict.

    A registration refreshes the inventory host that already goes by one of
    its names (see candidate_names); only when none does is a new host
    created.  `resolve` maps a list of addresses to {address: name}.
    """
    now = now or datetime.datetime.utcnow()
    by_uri = {h.get("uri"): h for h in hosts if h.get("uri")}
    pairs = [(s, by_uri.get(_first(s, "service-host"))) for s in services]
    ptr = resolve([a for s, h in pairs for a in addresses_of(s, h)]) if resolve else {}
    existing = {h.hostname: h for h in session.query(m.Host)}
    seen = {}
    unnamed = 0
    for service, host in pairs:
        names = candidate_names(service, host, ptr)
        if not names:
            unnamed += 1
            continue
        name = next((n for n in names if n in existing), names[0])
        fields = describe(service, host)
        if name in seen:                     # the same name registered twice: merge what it offers
            fields["capabilities"] = sorted(set(fields["capabilities"]) | set(seen[name]["capabilities"]))
            if not host:
                fields = dict(seen[name], capabilities=fields["capabilities"])
        seen[name] = fields

    summary = {"registered": len(seen), "created": [], "updated": 0, "unchanged": 0,
               "unnamed": unnamed, "adopted": []}
    for name, fields in sorted(seen.items()):
        row = existing.get(name)
        if row is None:
            summary["created"].append(name)
            if not dry_run:
                session.add(m.Host(hostname=name, source="lookup", last_seen=now, **fields))
            continue
        changed = any(getattr(row, k) != v for k, v in fields.items() if v or k in ("capabilities",))
        summary["updated" if changed else "unchanged"] += 1
        if row.source != "lookup":
            summary["adopted"].append(name)      # added by hand earlier, now registering itself
        if not dry_run:
            for k, v in fields.items():
                if v or k == "capabilities":     # never blank out what we knew
                    setattr(row, k, v)
            row.source, row.last_seen = "lookup", now
    if not dry_run:
        session.add(m.AuditLog(actor="lookup-service", action="lookup-sync", object_type="system",
                               detail={k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()}))
        session.commit()
    return summary
