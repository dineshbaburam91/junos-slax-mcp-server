"""A self-contained fake junos-mcp-server for integration tests.

It speaks the real MCP stdio protocol and exposes the two tools our client
uses (execute_junos_command, load_and_commit_config). It emulates a device
filesystem under FAKE_JMCP_SANDBOX so the real base64 write + "slaxproc
--check" flow can be exercised end to end without a device.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-jmcp")

SANDBOX = Path(os.environ.get("FAKE_JMCP_SANDBOX", "/tmp/fake-jmcp"))

_WRITE_RE = re.compile(r"printf %s '([^']*)' \| base64 -d > (\S+)")
_CHECK_RE = re.compile(r"slaxproc --check (\S+)")


def _local(device_path: str) -> Path:
    return SANDBOX / device_path.lstrip("/")


def _inner_shell(command: str) -> str:
    # command looks like: start shell command "<inner>"
    first = command.find('"')
    last = command.rfind('"')
    return command[first + 1 : last] if first != -1 and last > first else ""


@mcp.tool()
def execute_junos_command(router_name: str, command: str, timeout: int = 360) -> str:
    """Emulate running a Junos op command / shell command on a device."""
    if command.startswith("start shell command"):
        inner = _inner_shell(command)

        write = _WRITE_RE.search(inner)
        if write:
            b64, path = write.group(1), write.group(2)
            local = _local(path)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(base64.b64decode(b64))
            return ""

        check = _CHECK_RE.search(inner)
        if check:
            local = _local(check.group(1))
            content = local.read_text() if local.exists() else ""
            if "version 1." in content and "INVALID" not in content:
                return ""  # slaxproc --check is silent on success
            return f"{local.name}: error: parse error (invalid SLAX)"

        return f"SHELL-OUTPUT: {inner}"

    if command.startswith("op "):
        return f"OP-OUTPUT: {command}"

    return f"CLI-OUTPUT: {command}"


@mcp.tool()
def load_and_commit_config(
    router_name: str,
    config_text: str,
    config_format: str = "set",
    commit_comment: str = "",
) -> str:
    """Emulate loading and committing configuration on a device."""
    return f"loaded ({config_format}): {config_text}\ncommit complete"


if __name__ == "__main__":
    mcp.run()
