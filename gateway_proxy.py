"""
Put Squidbrake in front of ANY app's MCP connector: Stripe, GitHub, Slack, Gmail, Linear, Notion,
a database, an internal tool... The agent sees the app's normal tools; every call is checked against
the rules, recorded, and held for a human when a rule says so, before it reaches the app.

    python gateway_proxy.py --app stripe -- npx -y @stripe/mcp --tools=all
    python gateway_proxy.py --app github -- docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server
    python gateway_proxy.py --app acme   -- python demo_apps_mcp.py        (the sandbox company)
    python gateway_proxy.py --app crm --url https://crm.example.com/mcp    (a remote MCP server)

Calls show up in the dashboard as <app>.<tool>, e.g. stripe.create_refund, so rules can target them.
The app's own credentials (e.g. STRIPE_SECRET_KEY) go in the agent's MCP config env as usual; they are
passed to the app, never to the gateway. Gateway settings: see gw_async.py.
Easiest setup:  python connect.py wrap --app NAME --agent claude-code -- <the app's MCP command>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager

from mcp import Client, types
from mcp.client.stdio import StdioServerParameters
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

import gw_async as gw
from gw_async import log

APP = "app"
_upstream: Client | None = None

def gateway_tools() -> list[types.Tool]:
    """The gateway's own tools, named after the app (acme_check_approval) so they never clash with another
    connector's in agents that keep one list of tools."""
    return [
        types.Tool(
            name=gw.CHECK_TOOL,
            description=f"Finish a {APP} call Squidbrake was holding for human approval (WAITING FOR HUMAN "
                        "APPROVAL): runs it if approved, reports if rejected, or keeps waiting a little longer.",
            input_schema={"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]},
        ),
        types.Tool(name=gw.DECISIONS_TOOL, description=gw.DECISIONS_HELP,
                   input_schema={"type": "object", "properties": {}}),
    ]


def replay(call: dict):
    async def run():
        return await _upstream.call_tool(call["tool"], call.get("args") or {})
    return run


def text_result(message: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=is_error)


def summarize(result: types.CallToolResult) -> dict:
    """What the gateway records about the app's answer (text is kept; images/files are noted, not stored)."""
    parts = []
    for c in result.content or []:
        parts.append(c.text if getattr(c, "type", "") == "text" else f"[{getattr(c, 'type', 'content')}]")
    out: dict = {"content": parts[0] if len(parts) == 1 else parts}
    if result.structured_content is not None:
        out["structured"] = result.structured_content
    return out


async def report(event_id: str, outcome) -> types.CallToolResult:
    status, value, ms = outcome
    if status == "error":  # couldn't reach the app, or it crashed
        await gw.gw_result(event_id, error=value, duration_ms=ms)
        return text_result(f"ERROR from {APP}: {value}", is_error=True)
    summary = summarize(value)
    err = None
    if value.is_error:
        err = summary["content"] if isinstance(summary["content"], str) else json.dumps(summary["content"])[:2000]
    await gw.gw_result(event_id, output=summary, error=err, duration_ms=ms)
    return value


async def list_tools(ctx, params) -> types.ListToolsResult:
    result = await _upstream.list_tools(cursor=params.cursor if params else None)
    tools = list(result.tools)
    if not (params and params.cursor):
        tools += gateway_tools()
    return types.ListToolsResult(tools=tools, next_cursor=result.next_cursor)


async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    args = params.arguments or {}
    on_text = lambda t: text_result(t, is_error=t.startswith("NOT RUN") or t.startswith("Unknown"))
    if params.name == gw.DECISIONS_TOOL:
        return text_result(await gw.recent_decisions())
    if params.name == gw.CHECK_TOOL:
        return await gw.check_approval(str(args.get("event_id", "")), on_done=report, on_text=on_text)

    call = {"tool": params.name, "args": args}
    return await gw.guard(f"{APP}.{params.name}", args, replay(call), on_done=report, on_text=on_text, kind="mcp",
                          call=call)


def upstream_target(args) -> StdioServerParameters | str:
    if args.url:
        return args.url
    if not args.command:
        sys.exit("give the app's MCP command after --, or --url for a remote server")
    return StdioServerParameters(command=args.command[0], args=args.command[1:], env=dict(os.environ))


@asynccontextmanager
async def lifespan(server, target):
    global _upstream
    async with Client(target) as upstream:
        _upstream = upstream
        tools = (await upstream.list_tools()).tools
        log(f"guarding '{APP}' ({len(tools)} tools) -> gateway {gw.GATEWAY_URL} as '{gw.SOURCE}' (session {gw.SESSION})")
        yield {}


async def amain(args) -> None:
    target = upstream_target(args)
    server = Server(
        f"gateway-{APP}",
        instructions=f"{APP} tools. " + gw.agent_rules(),
        on_list_tools=list_tools, on_call_tool=call_tool,
        lifespan=lambda s: lifespan(s, target),
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    global APP
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app", required=True, help="short name used in the dashboard and rules, e.g. stripe")
    p.add_argument("--url", help="a remote (streamable HTTP) MCP server instead of a command")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- then the app's MCP server command")
    args = p.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    APP = args.app
    gw.set_tool_prefix(APP)
    gw.set_replay(replay)
    gw.cleanup_pending()
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not gw.GATEWAY_API_KEY:
        log("warning: GATEWAY_API_KEY is not set; the gateway will reject calls unless auth is off")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
