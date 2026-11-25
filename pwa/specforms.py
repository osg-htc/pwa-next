"""Forms and validation driven by pScheduler's own schemas.

pScheduler 5.3 ships a JSON Schema for every test and archiver plugin
(`spec-jsonschema.json`, one entry per spec version).  Copies live in
pwa/specs/.  From them this module derives

  * the fields to show for a test type, with titles, help text, drop-down
    choices and numeric limits;
  * validation of a spec before it is saved or published;
  * the lowest spec `schema` number that allows the fields in use.

So a new test type or option needs a new schema file, not new code.
"""
import copy
import functools
import json
import os
import re

import jsonschema

SPEC_DIR = os.path.join(os.path.dirname(__file__), "specs")

DURATION_RE = (r"^P(?!$)(\d+(\.\d+)?Y)?(\d+(\.\d+)?M)?(\d+(\.\d+)?W)?(\d+(\.\d+)?D)?"
               r"(T(?=\d)(\d+(\.\d+)?H)?(\d+(\.\d+)?M)?(\d+(\.\d+)?S)?)?$")

# The shared types the plugin schemas refer to as "#/pScheduler/<Name>".
# These mirror pscheduler/jsonval.py closely enough to drive forms and catch
# mistakes early; pScheduler itself remains the final judge.
PS_TYPES = {
    "AnyJSON": {},
    "Boolean": {"type": "boolean"},
    "Cardinal": {"type": "integer", "minimum": 1},
    "CardinalZero": {"type": "integer", "minimum": 0},
    "Duration": {"type": "string", "pattern": DURATION_RE,
                 "x-kind": "duration"},
    "Host": {"type": "string", "minLength": 1},
    "Integer": {"type": "integer"},
    "IPAddress": {"type": "string", "minLength": 1},
    "IPCIDR": {"type": "string", "minLength": 1},
    "IPv6CIDR": {"type": "string", "minLength": 1},
    "IPPort": {"type": "integer", "minimum": 0, "maximum": 65535},
    "IPPortRange": {"type": "object"},
    "IPTOS": {"type": "integer", "minimum": 0, "maximum": 255, "title": "IP TOS",
              "description": "Type-of-service byte to set on test packets (0-255)."},
    "IPVersion": {"type": "integer", "enum": [4, 6], "title": "IP version",
                  "description": "Force the test to use IPv4 or IPv6."},
    "IPv6FlowLabel": {"type": "integer", "minimum": 0, "maximum": 1048575,
                      "title": "IPv6 flow label",
                      "description": "Flow label to set on IPv6 test packets (0-1048575)."},
    "JQTransformSpecification": {"type": "object"},
    "Number": {"type": "number"},
    "Probability": {"type": "number", "minimum": 0, "maximum": 1},
    "RetryPolicy": {"type": "array", "items": {
        "type": "object",
        "properties": {"attempts": {"type": "integer", "minimum": 1},
                       "wait": {"type": "string", "pattern": DURATION_RE}},
        "required": ["attempts", "wait"], "additionalProperties": False}},
    "Schema": {"type": "integer", "minimum": 1},
    "SNMPOID": {"type": "string", "minLength": 1},
    "SNMPNumericOID": {"type": "string", "minLength": 1},
    "String": {"type": "string"},
    "URL": {"type": "string", "minLength": 1},
    "URLHostPort": {"type": "string", "minLength": 1},
}

# Filled in from the mesh (who tests to whom), never typed by a person.
PARTICIPANT_FIELDS = {"source", "dest", "source-node", "dest-node", "schema"}

# Shown first, in this order; everything else follows alphabetically under
# "More options".  Purely presentational.
COMMON_FIELDS = {
    "throughput": ["duration", "omit", "ip-version", "parallel", "udp", "bandwidth",
                   "congestion", "flow-label", "reverse"],
    "latency": ["packet-count", "packet-interval", "bucket-width", "ip-version",
                "packet-padding", "protocol"],
    "latencybg": ["packet-count", "packet-interval", "bucket-width", "ip-version",
                  "packet-padding", "duration"],
    "rtt": ["count", "interval", "length", "ip-version", "protocol", "ttl"],
    "trace": ["probe-type", "ip-version", "first-ttl", "hops", "length", "wait", "as"],
}

# Tools that can run each of the common test types (pScheduler picks the first
# one both ends support).
TOOLS = {
    "throughput": ["iperf3", "iperf2", "nuttcp", "ethr"],
    "latency": ["owping", "twping", "halfping"],
    "latencybg": ["powstream"],
    "rtt": ["ping", "twping", "tcpping"],
    "trace": ["traceroute", "tracepath", "paris-traceroute"],
}

FRIENDLY_TYPES = {
    "throughput": "Throughput", "latency": "One-way latency (scheduled)",
    "latencybg": "One-way latency (continuous)", "rtt": "Round-trip time (ping)",
    "trace": "Traceroute",
}


class SpecError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


@functools.lru_cache(maxsize=None)
def _load(kind, name):
    path = os.path.join(SPEC_DIR, kind, name + ".json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@functools.lru_cache(maxsize=None)
def available(kind="tests"):
    names = [f[:-5] for f in os.listdir(os.path.join(SPEC_DIR, kind)) if f.endswith(".json")]
    common = [n for n in FRIENDLY_TYPES if n in names] if kind == "tests" else []
    return common + sorted(n for n in names if n not in common)


def known(kind, name):
    return name in available(kind)


@functools.lru_cache(maxsize=None)
def _versions(kind, name):
    """Return (document, versions) with every version's properties spelled out.

    In the plugin files a property set to null means "same as in the previous
    version", so later versions only describe what they add.
    """
    doc = _load(kind, name)
    versions, previous = [], {}
    for v in doc["versions"]:
        if not isinstance(v, dict):
            versions.append(None)
            continue
        v = dict(v)
        props = {}
        for pname, node in (v.get("properties") or {}).items():
            if node is None:
                node = previous.get(pname)
            if isinstance(node, dict):
                props[pname] = node
        if "properties" in v:
            v["properties"] = props
        previous = props
        versions.append(v)
    return doc, versions


def _resolve(doc, node):
    """Follow $ref chains into #/local and #/pScheduler, merging as we go."""
    seen = 0
    node = dict(node)
    while "$ref" in node and seen < 10:
        ref = node.pop("$ref")
        parts = ref.lstrip("#/").split("/")
        if parts[0] == "pScheduler":
            base = PS_TYPES.get(parts[1], {})
            base = dict(base, **{"x-type": parts[1]})
        else:
            base = doc
            for p in parts:
                base = base.get(p, {}) if isinstance(base, dict) else {}
        node = dict(copy.deepcopy(base), **node)     # the referring node wins
        seen += 1
    node.pop("#", None)
    return node


def _title(name):
    small = {"ip": "IP", "ttl": "TTL", "tos": "TOS", "mss": "MSS", "udp": "UDP", "tcp": "TCP",
             "as": "AS", "cpu": "CPU", "rtt": "RTT", "dscp": "DSCP", "url": "URL", "mtu": "MTU"}
    return " ".join(small.get(w, w.capitalize()) for w in name.split("-"))


def properties(kind, name):
    """Names of every option the newest spec version of a plugin allows."""
    if not known(kind, name):
        return set()
    _, versions = _versions(kind, name)
    return set(((versions[-1] or {}).get("properties") or {}).keys())


def fields(kind, name, version=None):
    """Describe the editable fields of a spec as a list of dicts.

    Each has: name, title, help, kind (select|bool|number|duration|text|json),
    choices, minimum, maximum, integer, common.
    """
    doc, versions = _versions(kind, name)
    schema = versions[version or len(versions) - 1] or {}
    props = schema.get("properties") or {}
    common = COMMON_FIELDS.get(name, []) if kind == "tests" else []
    out = []
    for pname, raw in props.items():
        if kind == "tests" and pname in PARTICIPANT_FIELDS:
            continue
        if pname == "schema" or not isinstance(raw, dict):
            continue
        node = _resolve(doc, raw)
        f = {"name": pname, "title": node.get("title") or _title(pname),
             "help": " ".join(x for x in (node.get("description"),
                                           node.get("x-description-append")) if x),
             "choices": None, "minimum": node.get("minimum"), "maximum": node.get("maximum"),
             "integer": node.get("type") == "integer", "common": pname in common}
        typ = node.get("type")
        if "enum" in node:
            f["kind"], f["choices"] = "select", list(node["enum"])
        elif typ == "boolean":
            f["kind"] = "bool"
        elif typ in ("integer", "number"):
            f["kind"] = "number"
        elif node.get("x-kind") == "duration":
            f["kind"] = "duration"
        elif typ == "string":
            f["kind"] = "text"
        else:
            f["kind"] = "json"
        if pname.startswith("_"):
            f["title"] = f["title"] if node.get("title") else _title(pname.lstrip("_"))
        out.append(f)
    order = {n: i for i, n in enumerate(common)}
    out.sort(key=lambda f: (0, order[f["name"]]) if f["name"] in order else (1, f["name"]))
    return out


def parse_form(kind, name, form, prefix="spec."):
    """Turn submitted form values into a typed spec.  Raises SpecError."""
    spec, errors = {}, []
    for f in fields(kind, name):
        raw = form.get(prefix + f["name"])
        if f["kind"] == "bool":
            if raw in ("on", "true", "1"):
                spec[f["name"]] = True
            continue
        if raw is None or str(raw).strip() == "":
            continue
        raw = str(raw).strip()
        try:
            if f["kind"] == "number" or (f["kind"] == "select" and
                                         all(isinstance(c, (int, float)) for c in f["choices"])):
                val = float(raw)
                spec[f["name"]] = int(val) if val == int(val) else val
            elif f["kind"] == "duration":
                spec[f["name"]] = normalize_duration(raw)
            elif f["kind"] == "json":
                spec[f["name"]] = json.loads(raw)
            else:
                spec[f["name"]] = raw
        except ValueError:
            errors.append("%s: “%s” is not a valid %s" % (
                f["title"], raw, {"number": "number", "duration": "duration",
                                  "json": "JSON value"}.get(f["kind"], "value")))
    if errors:
        raise SpecError(errors)
    return spec


_UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400}


def normalize_duration(text):
    """Accept ISO 8601 ("PT30S") or shorthand ("30s", "6h", "90") and return ISO."""
    text = text.strip()
    if re.match(DURATION_RE, text.upper()):
        return text.upper()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([a-zA-Z]*)$", text)
    if not m or (m.group(2) and m.group(2).lower() not in _UNITS):
        raise ValueError(text)
    return seconds_to_iso(float(m.group(1)) * _UNITS.get(m.group(2).lower() or "s"))


def seconds_to_iso(seconds):
    """ISO 8601 duration for a number of seconds (PT0S, PT25S, PT1M30S, PT6H, P1D)."""
    seconds = float(seconds)
    if seconds == 0:
        return "PT0S"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    out = "P" + ("%dD" % days if days else "")
    time = ("%dH" % hours if hours else "") + ("%dM" % minutes if minutes else "")
    if secs:
        time += ("%.3f" % secs).rstrip("0").rstrip(".") + "S"
    return out + ("T" + time if time else "")


def iso_to_seconds(text):
    m = re.match(DURATION_RE, text or "")
    if not m:
        raise ValueError(text)
    g = lambda i: float(m.group(i)[:-1]) if m.group(i) else 0.0
    return (g(1) * 31536000 + g(3) * 2592000 + g(5) * 604800 + g(7) * 86400 +
            g(10) * 3600 + g(12) * 60 + g(14))


def schema_version(kind, name, spec):
    """Lowest spec version whose schema allows every key in `spec`."""
    _, versions = _versions(kind, name)
    keys = set(spec) - {"schema"}
    for n, v in enumerate(versions):
        if v and keys <= set((v.get("properties") or {}).keys()):
            return n
    return len(versions) - 1


# Option names that older tools (PWA, MeshConfig) used for something that exists under another name.
ALIASES = {"streams": "parallel", "tos-bits": "ip-tos", "omit-interval": "omit", "waittime": "sendwait",
           "sample-count": "packet-count", "ipv4-only": "ip-version", "ipv6-only": "ip-version"}


def unknown_option_message(kind, name, option, allowed):
    import difflib
    what = "%s tests" % name if kind == "tests" else "the %s archiver" % name
    guess = ALIASES.get(option) if ALIASES.get(option) in allowed else None
    if not guess:
        close = difflib.get_close_matches(option, sorted(allowed - {"schema"}), n=1, cutoff=0.6)
        guess = close[0] if close else None
    return "“%s” is not an option of %s%s" % (option, what, "; did you mean “%s”?" % guess if guess else "")


def validate(kind, name, spec):
    """Return a list of human-readable problems (empty when the spec is fine)."""
    if not known(kind, name):
        return []                   # unknown plugin: nothing to check against
    doc, versions = _versions(kind, name)
    version = schema_version(kind, name, spec)
    full = dict(spec, schema=version)
    if kind == "tests":
        props = (versions[version] or {}).get("properties") or {}
        for k, sample in (("source", "a.example.org"), ("dest", "b.example.org")):
            if k in props:
                full.setdefault(k, sample)
    schema = dict(versions[version] or {})
    schema["local"] = doc.get("local", {})
    schema["pScheduler"] = PS_TYPES
    errors = []
    for e in sorted(jsonschema.Draft7Validator(schema).iter_errors(full), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in e.path)
        msg = e.message
        if e.validator == "additionalProperties":
            allowed = set((schema.get("properties") or {}).keys())
            unknown = sorted(set(e.instance) - allowed) if isinstance(e.instance, dict) else []
            msg = "; ".join(unknown_option_message(kind, name, u, allowed) for u in unknown) or msg
        limits = {"maximum": "at most", "minimum": "at least", "exclusiveMaximum": "less than",
                  "exclusiveMinimum": "more than"}
        if e.validator in limits:
            bound = e.validator_value
            if e.validator in ("maximum", "minimum") and (e.schema or {}).get(
                    "exclusive" + e.validator.capitalize()) is True:        # draft-04 style
                bound_word = "less than" if e.validator == "maximum" else "more than"
            else:
                bound_word = limits[e.validator]
            bound = int(bound) if isinstance(bound, float) and bound == int(bound) else bound
            msg = "must be %s %s (it is %s)" % (bound_word, bound, e.instance)
        elif e.validator == "enum":
            msg = "must be one of %s (it is %r)" % (", ".join(str(x) for x in e.validator_value), e.instance)
        elif e.validator == "type":
            msg = "must be %s (it is %r)" % ({"integer": "a whole number", "number": "a number",
                                              "string": "text", "boolean": "true or false",
                                              "object": "a JSON object", "array": "a list"}.get(
                                              e.validator_value, e.validator_value), e.instance)
        if e.validator == "pattern" and "P(?!$)" in str(e.validator_value):
            msg = "%r is not a duration (use ISO 8601 such as PT30S, or 30s / 5m / 6h)" % e.instance
        errors.append("%s: %s" % (where, msg) if where else msg)
    return errors
