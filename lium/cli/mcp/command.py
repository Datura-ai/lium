"""`lium mcp`: a Model Context Protocol server over stdio, so an agent host can rent and use Lium GPUs."""

import click

INSTRUCTIONS = (
    "Rent NVIDIA GPU pods on Lium (lium.io), billed per second with no minimum. "
    "Loop: list_gpus -> rent_pod -> run_command (as often as needed) -> remove_pod. "
    "A pod costs money until remove_pod; check get_balance first."
)


def build_server(tools=None):
    """The server with every tool registered; mcp 2.x calls the class MCPServer, 1.x FastMCP."""
    try:
        from mcp.server import MCPServer as Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server

    from .tools import LiumTools, tool_functions

    server = Server(name="lium", instructions=INSTRUCTIONS)
    for function in tool_functions(tools or LiumTools()):
        server.tool()(function)
    return server


@click.command("mcp")
def mcp_command():
    """Serve Lium as an MCP server over stdio (tools: list_gpus, rent_pod, list_pods, run_command, remove_pod, get_balance).

    \b
    Add to an MCP host, e.g. Claude Desktop or Cursor:
      {"mcpServers": {"lium": {"command": "lium", "args": ["mcp"]}}}
    Auth is the API key `lium init` saved, or LIUM_API_KEY.
    """
    try:
        server = build_server()
    except ImportError:
        raise click.ClickException("The MCP server needs the mcp package: pip install 'lium.io[mcp]'")
    # stdout is the protocol channel: nothing else may print to it
    server.run(transport="stdio")
