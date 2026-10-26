"""Entry point for the Junos SLAX / XML RPC MCP server."""

from server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
