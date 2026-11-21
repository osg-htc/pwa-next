"""What each test type is for, and which tools support which options.

Two data files, both in pwa/specs/:
  catalog.json   measured: built by tools/build_test_catalog.py from a live
                 pScheduler (which tools exist, and which accept each option)
  guidance.json  written by hand: what the test is for, how it is addressed
"""
import functools
import json
import os

from . import specforms

AVAILABILITY = {
    "standard": "Installed on every perfSONAR 5.2 testpoint and toolkit.",
    "extras": "In pScheduler's optional 'extras' bundle: hosts have it only if their site installed it.",
    "snmp": "In pScheduler's optional 'snmp' bundle: hosts have it only if their site installed it.",
    "unreleased": "In the pScheduler source but not released.",
    "new": "Arrives with perfSONAR 5.3.",
    "varies": "Built only on some platforms.",
}
ADDRESSING = {
    "pair": "between two hosts: one host tests to another",
    "each": "on each host by itself, against a target that is not another host in the group",
    "none": "not addressed by host; cannot be used in a task here",
}
USE = {"mesh": "measurement meshes", "special": "particular monitoring jobs", "testing": "testing pScheduler only"}
SCHEDULING = {
    "exclusive": "Exclusive: a host runs only one at a time, and nothing else except background tests.",
    "normal": "Normal: does not overlap other normal or exclusive tests.",
    "background": "Background: runs alongside anything.",
    "background-multi": "Background, continuous: runs alongside anything and reports repeatedly.",
}


@functools.lru_cache(maxsize=None)
def _load(name):
    path = os.path.join(specforms.SPEC_DIR, name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def catalog():
    return _load("catalog.json")


def guidance(test_type):
    return _load("guidance.json").get(test_type) or {}


def addressing(test_type):
    return guidance(test_type).get("addressing") or ("pair" if test_type in specforms.FRIENDLY_TYPES else "each")


def title(test_type):
    return guidance(test_type).get("title") or specforms.FRIENDLY_TYPES.get(test_type, test_type)


def measured(test_type):
    """The catalog entry for a test type, or {} if the reference pScheduler does not have it."""
    return (catalog().get("tests") or {}).get(test_type) or {}


def tool_description(tool):
    return ((catalog().get("tools") or {}).get(tool) or {}).get("description") or ""


def tools(test_type):
    """Tools for a test type, most preferred first."""
    return measured(test_type).get("tools") or specforms.TOOLS.get(test_type, [])


def option_tools(test_type, option):
    """Tools that accept an option, or None if that is not known."""
    m = measured(test_type)
    if option in (m.get("options") or {}):
        return m["options"][option]
    return None


def option_note(test_type, option):
    """Short text for an option that not every tool supports, else ''."""
    allowed = option_tools(test_type, option)
    every = tools(test_type)
    if allowed is None or set(allowed) >= set(every):
        return ""
    if not allowed:
        return "no tool in pScheduler 5.2 accepts this option"
    return "only " + ", ".join(allowed)


def option_value_tools(test_type, option):
    return (measured(test_type).get("option_values") or {}).get(option) or {}


def problems(test_type, spec, chosen_tools):
    """Option/tool combinations pScheduler will refuse.  Returns a list of messages."""
    out = []
    every = tools(test_type)
    candidates = list(chosen_tools) if chosen_tools else list(every)
    if not measured(test_type):
        return out
    usable = set(candidates)
    for option, value in (spec or {}).items():
        allowed = option_tools(test_type, option)
        by_value = option_value_tools(test_type, option)
        if by_value and str(value) in by_value:
            allowed = by_value[str(value)]
        if allowed is None:
            continue
        usable &= set(allowed)
        if not set(allowed) & set(candidates):
            if not allowed:
                out.append("“%s” is not accepted by any tool in pScheduler 5.2." % option)
            elif chosen_tools:
                out.append("“%s”%s works only with %s, which is not among the chosen tools (%s)." % (
                    option, " = %s" % value if by_value else "", ", ".join(allowed), ", ".join(candidates)))
    if not out and not usable:
        out.append("No single tool supports this combination of options (%s)." % ", ".join(
            o for o in spec if option_tools(test_type, o) is not None))
    return out
