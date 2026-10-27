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
from concurrent.futures import ThreadPoolExecutor

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
    # jmcp loads block.cmd/block.cfg from its cwd first, so keep them next to the inventory.
    cwd = os.path.dirname(devices) if os.path.isabs(devices) else None
    return StdioServerParameters(
        command=command, args=args, env=dict(os.environ), cwd=cwd
    )


def _run(coro):
    """Run a coroutine to completion, even if an event loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # FastMCP invokes sync tools on its loop thread, so use a fresh loop elsewhere.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _result_text(result) -> str:
    """Flatten an MCP CallToolResult's content blocks into text."""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts).strip()


async def _call_tools_async(calls: list[tuple[str, dict]]) -> list[str]:
    params = _server_params()
    outputs: list[str] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                text = _result_text(result)
                if getattr(result, "isError", False):
                    text = f"ERROR: {text}"
                outputs.append(text)
    return outputs


def call_tools(calls: list[tuple[str, dict]]) -> list[str]:
    """Call several jmcp tools over one session, so the device sees one SSH login."""
    return _run(_call_tools_async(calls))


def call_tool(name: str, arguments: dict) -> str:
    """Synchronously call a jmcp tool and return its text output."""
    return call_tools([(name, arguments)])[0]


async def _list_tools_async() -> list[str]:
    params = _server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [t.name for t in tools.tools]


def list_tools() -> list[str]:
    """Return the tool names advertised by jmcp (connectivity check)."""
    return _run(_list_tools_async())


# --- convenience wrappers over specific jmcp tools -----------------------------


def command_call(router: str, command: str, timeout: int = 360) -> tuple[str, dict]:
    return (
        "execute_junos_command",
        {"router_name": router, "command": command, "timeout": timeout},
    )


def commit_call(
    router: str,
    config_text: str,
    config_format: str = "set",
    commit_comment: str = "SLAX script deploy via junos-slax-mcp",
) -> tuple[str, dict]:
    return (
        "load_and_commit_config",
        {
            "router_name": router,
            "config_text": config_text,
            "config_format": config_format,
            "commit_comment": commit_comment,
        },
    )


def run_command(router: str, command: str, timeout: int = 360) -> str:
    """Run a Junos operational command on a device via jmcp."""
    return call_tool(*command_call(router, command, timeout))


def run_commands(router: str, commands: list[str], timeout: int = 360) -> list[str]:
    """Run several operational commands on a device over one jmcp session."""
    return call_tools([command_call(router, c, timeout) for c in commands])


def load_and_commit(
    router: str,
    config_text: str,
    config_format: str = "set",
    commit_comment: str = "SLAX script deploy via junos-slax-mcp",
) -> str:
    """Load and commit configuration on a device via jmcp."""
    return call_tool(*commit_call(router, config_text, config_format, commit_comment))
