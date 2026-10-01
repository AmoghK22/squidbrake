"""
The `squidbrake` command (installed with pip):

  squidbrake                         start the gateway (same as: python server.py)
  squidbrake connect all             connect every AI agent on this computer (same as: python connect.py ...)
  squidbrake connect claude-code     connect one agent
  squidbrake connect status          which agents are covered (and whether Codex has trusted the hook)
  squidbrake hook                    the Claude Code hook, used by the Claude Code plugin (plugin/)
  squidbrake proxy --app NAME -- CMD an MCP server that checks every call to the app's MCP server CMD first
                                     (same as: python gateway_proxy.py ...)
  squidbrake pilot join CODE --server URL   share usage counts with a pilot (asks first; see pilot.py)
  squidbrake add-key NAME | keys | verify FILE | ...   see: squidbrake --help

Data, keys and rules.yaml live in ~/.squidbrake (set SQUIDBRAKE_HOME to move them).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    here = Path(__file__).resolve().parent
    # The gateway's modules sit next to this file in the package (in a checkout, one folder up)
    # and import each other by their plain names.
    sys.path.insert(0, str(here if (here / "server.py").exists() else here.parent))
    os.environ.setdefault("SQUIDBRAKE_CLI", "squidbrake")
    argv = sys.argv[1:]
    if argv[:1] in (["-V"], ["--version"]):
        from squidbrake import __version__
        print(f"squidbrake {__version__}")
        return 0
    if argv[:1] == ["connect"]:
        import connect
        connect.main(argv[1:])
        return 0
    if argv[:1] == ["hook"]:   # the Claude Code plugin's hook (reads the event on stdin)
        sys.argv = ["claude_hook.py", *argv[1:]]
        import claude_hook
        claude_hook.main()
        return 0
    if argv[:1] == ["proxy"]:  # an MCP server: put Squidbrake in front of any app's MCP server
        sys.argv = ["squidbrake proxy", *argv[1:]]
        import gateway_proxy
        gateway_proxy.main()
        return 0
    import server
    return server.main(argv)


if __name__ == "__main__":
    sys.exit(main())
