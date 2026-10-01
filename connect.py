"""
Connect an AI agent to Squidbrake in one command.
(Installed with pip? Type `squidbrake connect ...` wherever this says `python connect.py ...`.)

  python connect.py claude-code            Claude Code: every tool call goes through the gateway (hook)
                                           + database tools (MCP)
  python connect.py claude-code --remove   undo it
  python connect.py mcp --name antigravity Antigravity, Claude Desktop, Cursor, ...: prints the MCP config to paste
                                           (--install writes it into Antigravity's config for you)

Guard any app (Stripe, GitHub, Gmail, Slack, ... anything with an MCP server) for an agent:
  python connect.py wrap --agent claude-code --app stripe --env STRIPE_SECRET_KEY=sk_... -- npx -y @stripe/mcp --tools=all
  python connect.py wrap --agent antigravity --install --sandbox      (the built-in Acme sandbox company)

Options:
  --url URL      gateway address (default http://localhost:8080)
  --key KEY      use an existing agent key (default: create a new key named after the agent;
                 only works on the machine where the gateway runs)
  --project DIR  Claude Code: install for one project folder instead of for all your projects
  --yes          don't ask before changing Claude Code settings
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
HOOK = BASE_DIR / "claude_hook.py"
MCP_SERVER = BASE_DIR / "gateway_mcp.py"
MCP_NAME = "gateway-db"  # claude_hook.py skips mcp__gateway-db__* so these calls aren't recorded twice


LOCAL_URLS = ("http://localhost", "http://127.0.0.1", "http://[::1]")


def new_key(name: str, url: str = "http://localhost:8080") -> str:
    if not url.startswith(LOCAL_URLS):
        sys.exit(f"This gateway ({url}) runs on another machine, so its keys are made there.\n"
                 f"Ask its admin to add '{name}' as an AI agent in the dashboard's Team tab, then run this again with --key gw_...")
    import server  # the gateway's key store (data/keys.json next to it)
    if server.keystore.from_env:
        sys.exit("Keys come from GATEWAY_API_KEYS on this gateway. Add one there and pass it with --key.")
    existing = {k["name"] for k in server.keystore.listing()}
    final, n = name, 2
    while final in existing:
        final, n = f"{name}-{n}", n + 1
    key = server.keystore.add(final)
    with server.audited_tx() as conn:
        server.audit(conn, "command-line", "team.added", final, kind="agent", approver=False, roles=[], via="connect.py")
    print(f"Created gateway key '{final}' for this agent.")
    return key


def mcp_entry(url: str, key: str, source: str) -> dict:
    return {"command": PYTHON, "args": [str(MCP_SERVER)],
            "env": {"GATEWAY_URL": url, "GATEWAY_API_KEY": key, "GATEWAY_SOURCE": source}}


# --------------------------------------------------------------------------- claude code

def settings_path(project: str | None) -> Path:
    return (Path(project).resolve() / ".claude" / "settings.json") if project else Path.home() / ".claude" / "settings.json"


def strip_ours(settings: dict) -> dict:
    """Remove hook entries this script added earlier (so re-running doesn't duplicate them)."""
    hooks = settings.get("hooks", {})
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit"):
        groups = []
        for g in hooks.get(event, []):
            g["hooks"] = [h for h in g.get("hooks", []) if "claude_hook.py" not in json.dumps(h)]
            if g["hooks"]:
                groups.append(g)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return settings


def previous_key(settings: dict, url: str) -> str | None:
    """Re-running connect.py reuses the key it installed last time instead of piling up new ones."""
    for g in settings.get("hooks", {}).get("PreToolUse", []):
        for h in g.get("hooks", []):
            a = h.get("args", [])
            if any("claude_hook.py" in x for x in a) and "--key" in a and "--url" in a and a[a.index("--url") + 1] == url:
                return a[a.index("--key") + 1]
    return None


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def claude_code(args) -> None:
    path = settings_path(args.project)
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    claude = shutil.which("claude")
    scope = ["--scope", "project"] if args.project else ["--scope", "user"]

    if args.remove:
        if not confirm(f"Remove Squidbrake hooks from {path}?", args.yes):
            return
        path.write_text(json.dumps(strip_ours(settings), indent=2), encoding="utf-8")
        if claude:
            subprocess.run([claude, "mcp", "remove", MCP_NAME, *scope], cwd=args.project or None)
        print("Removed. Restart Claude Code to apply.")
        return

    where = f"the project {Path(args.project).resolve()}" if args.project else "ALL your Claude Code projects"
    print(f"This routes every Claude Code tool call in {where} through the gateway at {args.url},\n"
          f"and adds database tools. It changes {path} (a backup is kept).")
    if not confirm("Continue?", args.yes):
        return
    key = args.key or previous_key(settings, args.url) or new_key("claude-code", args.url)

    hook_cmd = {"type": "command", "command": PYTHON,
                "args": [str(HOOK), "--url", args.url, "--key", key, "--source", "claude-code"]}
    settings = strip_ours(settings)
    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("PreToolUse", []).append({"matcher": "*", "hooks": [{**hook_cmd, "timeout": 600}]})
    hooks.setdefault("PostToolUse", []).append({"matcher": "*", "hooks": [{**hook_cmd, "timeout": 30}]})
    # a tool that errored (e.g. a command that exited non-zero) reports here instead of PostToolUse
    hooks.setdefault("PostToolUseFailure", []).append({"matcher": "*", "hooks": [{**hook_cmd, "timeout": 30}]})
    # what you ask, so the gateway can tell what came from you and what came from a web page or an email
    hooks.setdefault("UserPromptSubmit", []).append({"hooks": [{**hook_cmd, "timeout": 15}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    print(f"Added the hook to {path}")

    entry = mcp_entry(args.url, key, "claude-code")
    cmd = [claude or "claude", "mcp", "add", "--transport", "stdio", MCP_NAME, *scope,
           *[x for k, v in entry["env"].items() for x in ("--env", f"{k}={v}")], "--", entry["command"], *entry["args"]]
    if claude:
        subprocess.run([claude, "mcp", "remove", MCP_NAME, *scope], cwd=args.project or None,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = subprocess.run(cmd, cwd=args.project or None).returncode == 0
        print("Added the database tools (MCP server 'gateway-db')." if ok else "Adding the MCP server failed; see above.")
    else:
        print("The `claude` command wasn't found, so add the database tools yourself with:\n  " + subprocess.list2cmdline(cmd))
    print("\nDone. Restart Claude Code, then try asking it:\n"
          '  "Using the gateway-db tools, show me the top 5 customers by revenue"\n'
          '  "Give every customer on the team plan a 15% discount"   <- waits for your approval\n'
          '  "Drop the orders table"                                <- blocked\n'
          f"Watch it all at {args.url}/dashboard")


# --------------------------------------------------------------------------- other MCP clients

WHERE = {
    "antigravity": "Antigravity: in the Agent panel open the '...' menu > MCP Servers > Manage MCP Servers > "
                   "View raw config, and merge this into mcp_config.json",
    "claude-desktop": "Claude Desktop: Settings > Developer > Edit Config, and merge this into claude_desktop_config.json",
    "cursor": "Cursor: merge this into ~/.cursor/mcp.json (or Settings > MCP > Add new server)",
}


def mcp(args) -> None:
    name = args.name
    key = args.key or existing_mcp_key(name, args.url) or new_key(name, args.url)
    deliver(name, MCP_NAME, mcp_entry(args.url, key, name), args,
            f"The agent then has list_tables / describe_table / query / execute tools; watch at {args.url}/dashboard")


# --------------------------------------------------------------------------- guard any app

ANTIGRAVITY_CONFIG = Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
PROXY = BASE_DIR / "gateway_proxy.py"
SANDBOX = BASE_DIR / "demo_apps_mcp.py"


def agent_configs() -> dict[str, Path]:
    """Where each agent keeps its MCP servers (only read here, to find a key we gave it before)."""
    appdata = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    desktop = (appdata / "Claude" if sys.platform == "win32" else
               Path.home() / "Library" / "Application Support" / "Claude" if sys.platform == "darwin" else
               Path.home() / ".config" / "Claude")
    return {"antigravity": ANTIGRAVITY_CONFIG, "cursor": Path.home() / ".cursor" / "mcp.json",
            "claude-desktop": desktop / "claude_desktop_config.json"}


def existing_mcp_key(agent: str, url: str) -> str | None:
    """Reuse the key this agent already has (from its MCP config) instead of making a new one."""
    path = agent_configs().get(agent)
    if path and path.exists():
        try:
            servers = json.loads(path.read_text(encoding="utf-8") or "{}").get("mcpServers", {})
        except ValueError:
            return None
        for s in servers.values():
            env = s.get("env", {})
            if env.get("GATEWAY_URL") == url and env.get("GATEWAY_API_KEY"):
                return env["GATEWAY_API_KEY"]
    return None


def install_json(path: Path, server_name: str, entry: dict) -> None:
    text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    data = json.loads(text) if text else {}
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    data.setdefault("mcpServers", {})[server_name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def deliver(agent: str, server_name: str, entry: dict, args, done: str) -> None:
    """Install the MCP server for the agent, or print the config to paste."""
    if agent == "claude-code":
        claude = shutil.which("claude")
        scope = ["--scope", "project"] if args.project else ["--scope", "user"]
        cmd = [claude or "claude", "mcp", "add", "--transport", "stdio", server_name, *scope,
               *[x for k, v in entry["env"].items() for x in ("--env", f"{k}={v}")], "--", entry["command"], *entry["args"]]
        if not claude:
            print("The `claude` command wasn't found; add it yourself with:\n  " + subprocess.list2cmdline(cmd))
            return
        subprocess.run([claude, "mcp", "remove", server_name, *scope], cwd=args.project or None,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if subprocess.run(cmd, cwd=args.project or None).returncode == 0:
            print(f"Added MCP server '{server_name}' to Claude Code. Restart Claude Code. {done}")
        return
    if agent == "antigravity" and getattr(args, "install", False):
        install_json(ANTIGRAVITY_CONFIG, server_name, entry)
        print(f"Added '{server_name}' to {ANTIGRAVITY_CONFIG} (backup kept). Restart Antigravity. {done}")
        return
    print("\n" + WHERE.get(agent, "Add this to your agent's MCP configuration") + ", then restart the agent:\n")
    print(json.dumps({"mcpServers": {server_name: entry}}, indent=2))
    print("\n" + done)


def wrap(args) -> None:
    if args.sandbox:
        app, command = args.app or "acme", [PYTHON, str(SANDBOX)]
    else:
        command = [c for c in args.command if c != "--"]
        app = args.app
        if not app or not (command or args.app_url):
            sys.exit("give --app NAME and the app's MCP command after --  (or --app-url URL), or use --sandbox")
    key = args.key
    if not key and args.agent == "claude-code":
        path = settings_path(args.project)
        key = previous_key(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}, args.url)
    key = key or existing_mcp_key(args.agent, args.url) or new_key(args.agent, args.url)
    env = {"GATEWAY_URL": args.url, "GATEWAY_API_KEY": key, "GATEWAY_SOURCE": args.agent}
    for kv in args.env or []:  # the app's own credentials, e.g. STRIPE_SECRET_KEY=sk_live_...
        k, _, v = kv.partition("=")
        env[k] = v
    target = ["--url", args.app_url] if args.app_url else ["--", *command]
    entry = {"command": PYTHON, "args": [str(PROXY), "--app", app, *target], "env": env}
    # Claude Code's hook skips mcp__gw-* tools: the proxy already records them.
    deliver(args.agent, f"gw-{app}", entry, args,
            f"Its {app} tools now go through the gateway (shown as {app}.<tool>); watch at {args.url}/dashboard")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="squidbrake connect" if os.getenv("SQUIDBRAKE_CLI") else None,
                                description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("claude-code", "mcp", "wrap"):
        s = sub.add_parser(name)
        s.add_argument("--url", default="http://localhost:8080", help="the gateway's address")
        s.add_argument("--key")
        if name == "claude-code":
            s.add_argument("--project")
            s.add_argument("--remove", action="store_true")
            s.add_argument("--yes", action="store_true")
        elif name == "mcp":
            s.add_argument("--name", default="mcp-agent", help="antigravity, claude-desktop, cursor, or any label")
            s.add_argument("--install", action="store_true", help="antigravity: write it into its config")
            s.add_argument("--project")
        else:
            s.add_argument("--agent", required=True, help="claude-code, antigravity, claude-desktop, cursor, or any label")
            s.add_argument("--app", help="short name for the app, e.g. stripe, github, gmail")
            s.add_argument("--app-url", help="the app's remote MCP server URL (instead of a command)")
            s.add_argument("--sandbox", action="store_true", help="use the built-in Acme sandbox company")
            s.add_argument("--env", action="append", help="KEY=VALUE for the app's own credentials (repeatable)")
            s.add_argument("--project", help="claude-code: only for this project folder")
            s.add_argument("--install", action="store_true", help="antigravity: write it into its config")
            s.add_argument("command", nargs=argparse.REMAINDER, help="-- then the app's MCP server command")
    args = p.parse_args(argv)
    args.url = args.url.rstrip("/")
    if args.cmd == "mcp":
        args.agent = args.name
    {"claude-code": claude_code, "mcp": mcp, "wrap": wrap}[args.cmd](args)


if __name__ == "__main__":
    main()
