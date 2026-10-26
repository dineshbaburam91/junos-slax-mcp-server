"""SLAX script scaffolds and helpers for script generation.

These scaffolds encode the stable boilerplate every Junos SLAX script needs
(version line, namespace declarations, the junos.xsl import, and the correct
top-level match for each script type). The generator attaches scenario-specific
reference passages so the model can fill in the body correctly.
"""

from __future__ import annotations

# Standard namespace / import header shared by most Junos SLAX scripts.
_HEADER = """version 1.1;

ns junos = "http://xml.juniper.net/junos/*/junos";
ns xnm = "http://xml.juniper.net/xnm/1.1/xnm";
ns jcs = "http://xml.juniper.net/junos/commit-scripts/1.0";

import "../import/junos.xsl";
"""

SCRIPT_TYPES = ("op", "commit", "event", "snmp")

# Keyword hints used to infer the script type from a free-text scenario.
_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "commit": (
        "commit",
        "configuration",
        "validate config",
        "enforce",
        "transient change",
        "warning",
        "error on commit",
    ),
    "event": (
        "event",
        "event-policy",
        "trigger",
        "syslog",
        "on error",
        "snmp trap",
        "automatically run",
        "when an event",
    ),
    "snmp": (
        "snmp",
        "oid",
        "mib",
        "get-next",
        "unsupported oid",
    ),
    "op": (
        "op script",
        "operational",
        "cli command",
        "show ",
        "on demand",
        "run manually",
        "custom command",
    ),
}

_TEMPLATES: dict[str, str] = {
    "op": _HEADER
    + """
/* Declare CLI arguments shown in "op" command help. */
var $arguments = {
    <argument> {
        <name> "interface";
        <description> "Interface name to inspect";
    }
}
param $interface;

match / {
    <op-script-results> {
        /* Build and send an RPC, then act on the response. */
        var $rpc = <get-interface-information> {
            <interface-name> $interface;
        }
        var $result = jcs:invoke($rpc);

        if ($result//oper-status == "down") {
            <output> "Interface " _ $interface _ " is DOWN";
        } else {
            <output> "Interface " _ $interface _ " is up";
        }
    }
}
""",
    "commit": _HEADER
    + """
match configuration {
    /* Inspect candidate config and raise warnings/errors or make changes. */
    var $config = .;

    for-each (interfaces/interface) {
        var $ifname = name;
        if (not(description)) {
            <xnm:warning> {
                <message> "Interface " _ $ifname _ " has no description";
            }
        }
    }

    /* Example transient change (not saved to config, applied at commit): */
    /*
    <change> {
        <system> {
            <host-name> "example-host";
        }
    }
    */
}
""",
    "event": _HEADER
    + """
/*
 * Event scripts are invoked by an event-policy. Reference the triggering
 * event through the event-script-input tree.
 */
match / {
    <event-script-results> {
        var $event = event-script-input/trigger-event;

        <output> "Triggered by event: " _ $event/id;

        /* Example: collect state and log a response action. */
        var $rpc = <get-system-uptime-information>;
        var $uptime = jcs:invoke($rpc);

        <syslog> {
            <message> "event-script ran for " _ $event/id;
        }
    }
}
""",
    "snmp": _HEADER
    + """
/*
 * SNMP (op) script to serve a custom/unsupported OID. Registered under
 * [ system scripts op ] and referenced from an SNMP MIB mapping.
 */
param $snmp_action;   /* "get" or "get-next" */
param $snmp_oid;

match / {
    <snmp-script-results> {
        var $rpc = <get-system-uptime-information>;
        var $data = jcs:invoke($rpc);

        <snmp-object> {
            <name> $snmp_oid;
            <value> $data//up-time/@junos:seconds;
            <type> "Counter32";
        }
    }
}
""",
}

# Short apply/install note per script type for the generated response.
INSTALL_NOTES: dict[str, str] = {
    "op": "Install under [ system scripts op file <name>.slax ]; run with "
    '"op <name>". Arguments map to CLI options.',
    "commit": "Install under [ system scripts commit file <name>.slax ]; runs "
    "automatically on every commit.",
    "event": "Install under [ event-options event-script file <name>.slax ] and "
    "bind it from an [ event-options policy ... then event-script ].",
    "snmp": "Install as an op script and map the OID under "
    "[ system scripts op ] plus the SNMP MIB mapping.",
}

# On-device scripts directory per script type.
SCRIPT_DIRS: dict[str, str] = {
    "op": "/var/db/scripts/op",
    "commit": "/var/db/scripts/commit",
    "event": "/var/db/scripts/event",
    "snmp": "/var/db/scripts/snmp",
}


def deploy_target(script_type: str, filename: str) -> tuple[str, str]:
    """Return (remote_dir, config_set_line) for installing a script.

    The config statement differs by type: op/commit/snmp live under
    [ system scripts ... ] while event scripts live under [ event-options ].
    """
    remote_dir = SCRIPT_DIRS.get(script_type, SCRIPT_DIRS["op"])
    if script_type == "event":
        config_line = f"set event-options event-script file {filename}"
    else:
        kind = script_type if script_type in ("op", "commit", "snmp") else "op"
        config_line = f"set system scripts {kind} file {filename}"
    return remote_dir, config_line



def infer_script_type(scenario: str) -> str:
    """Guess the SLAX script type from a scenario description."""
    low = scenario.lower()
    scores = {kind: 0 for kind in SCRIPT_TYPES}
    for kind, hints in _TYPE_HINTS.items():
        for hint in hints:
            if hint in low:
                scores[kind] += 1
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "op"


def scaffold_for(script_type: str) -> str:
    """Return the boilerplate scaffold for a script type (defaults to op)."""
    return _TEMPLATES.get(script_type, _TEMPLATES["op"])
