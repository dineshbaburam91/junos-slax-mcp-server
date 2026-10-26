"""Thin MCP client to the junos-mcp-server (jmcp).

Instead of re-implementing device connectivity, this module launches
junos-mcp-server over stdio and calls its tools (execute_junos_command,
load_and_commit_config) as an MCP client. The jmcp location, device
inventory file, and launch command are configurable via environment.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Optional overrides. junos-mcp-server is a project dependency, so by default
# we launch its installed "junos-mcp-server" console script; no path is hardcoded.
JMCP_DIR = os.environ.get("JUNOS_MCP_DIR")  # optional dev checkout to run instead
JMCP_DEVICES = os.environ.get("JUNOS_MCP_DEVICES", "devices.json")


def _jmcp_module_dir() -> str | None:
    try:
        import jmcp

        return os.path.dirname(os.path.abspath(jmcp.__file__))
    except Exception:  # noqa: BLE001 - package may not be importable
        return None


def _devices_path() -> str:
    """Resolve the inventory file, falling back to the installed jmcp dir."""
    path = JMCP_DEVICES
    if os.path.isabs(path) or os.path.exists(path):
        return os.path.abspath(path)
    base = JMCP_DIR or _jmcp_module_dir()
    if base:
        candidate = os.path.join(base, path)
        if os.path.exists(candidate):
            return candidate
    return path


def _server_params() -> StdioServerParameters:
    """Build the stdio launch command for jmcp.

    Resolution order: an explicit JUNOS_MCP_CMD override, an explicit
    JUNOS_MCP_DIR checkout, the installed "junos-mcp-server" console script,
    then the installed jmcp module run with the current interpreter.
    """
    devices = _devices_path()
    stdio_args = ["-t", "stdio", "-f", devices]

    override = os.environ.get("JUNOS_MCP_CMD")
    if override:
        parts = override.split()
        command, args = parts[0], parts[1:]
    elif JMCP_DIR:
        command = sys.executable
        args = [os.path.join(JMCP_DIR, "jmcp.py"), *stdio_args]
    elif shutil.which("junos-mcp-server"):
        command = "junos-mcp-server"
        args = stdio_args
    else:
        module_dir = _jmcp_module_dir()
        if not module_dir:
            raise RuntimeError(
                "junos-mcp-server is not installed and no JUNOS_MCP_DIR/"
                "JUNOS_MCP_CMD override was provided."
            )
        command = sys.executable
        args = [os.path.join(module_dir, "jmcp.py"), *stdio_args]
    return StdioServerParameters(command=command, args=args, env=dict(os.environ))


def _result_text(result) -> str:
    """Flatten an MCP CallToolResult's content blocks into text."""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts).strip()


async def _call_tool_async(name: str, arguments: dict) -> str:
    params = _server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            text = _result_text(result)
            if getattr(result, "isError", False):
                return f"ERROR: {text}"
            return text


def call_tool(name: str, arguments: dict) -> str:
    """Synchronously call a jmcp tool and return its text output."""
    return asyncio.run(_call_tool_async(name, arguments))


async def _list_tools_async() -> list[str]:
    params = _server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [t.name for t in tools.tools]


def list_tools() -> list[str]:
    """Return the tool names advertised by jmcp (connectivity check)."""
    return asyncio.run(_list_tools_async())


# --- convenience wrappers over specific jmcp tools -----------------------------


def run_command(router: str, command: str, timeout: int = 360) -> str:
    """Run a Junos operational command on a device via jmcp."""
    return call_tool(
        "execute_junos_command",
        {"router_name": router, "command": command, "timeout": timeout},
    )


def load_and_commit(
    router: str,
    config_text: str,
    config_format: str = "set",
    commit_comment: str = "SLAX script deploy via junos-slax-mcp",
) -> str:
    """Load and commit configuration on a device via jmcp."""
    return call_tool(
        "load_and_commit_config",
        {
            "router_name": router,
            "config_text": config_text,
            "config_format": config_format,
            "commit_comment": commit_comment,
        },
    )
