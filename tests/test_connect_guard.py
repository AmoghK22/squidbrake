"""connect.py guard: route an agent's own MCP servers through Squidbrake, and put them back exactly."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import connect  # noqa: E402

URL, KEY = "https://acme.app.example.com", "gw_agent_test"


def _home(tmp_path, monkeypatch):
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    return tmp_path


def test_guard_and_restore(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir(parents=True)
    original = {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_TOKEN": "t"}},
        "linear": {"url": "https://mcp.linear.app/sse"},
        "private-api": {"url": "https://api.internal/mcp", "headers": {"Authorization": "Bearer x"}},
    }, "otherSetting": True}
    cursor.write_text(json.dumps(original), encoding="utf-8")
    vscode = connect.mcp_configs()["vscode"][0][0]
    vscode.parent.mkdir(parents=True)
    vscode.write_text(json.dumps({"servers": {"db": {"type": "stdio", "command": "uvx", "args": ["mcp-db"]}}}), encoding="utf-8")

    connect.main(["guard", "--agent", "all", "--url", URL, "--key", KEY, "--yes"])
    g = json.loads(cursor.read_text(encoding="utf-8"))
    gh = g["mcpServers"]["github"]
    assert gh["args"][:3] == [str(connect.PROXY), "--app", "github"] and gh["args"][3:] == ["--", "npx", "-y", "@modelcontextprotocol/server-github"]
    assert gh["env"] == {"GITHUB_TOKEN": "t", "GATEWAY_URL": URL, "GATEWAY_API_KEY": KEY, "GATEWAY_SOURCE": "cursor"}
    assert g["mcpServers"]["linear"]["args"][-2:] == ["--url", "https://mcp.linear.app/sse"]
    assert g["mcpServers"]["private-api"] == original["mcpServers"]["private-api"]      # can't carry its headers: left alone
    assert g["otherSetting"] is True
    v = json.loads(vscode.read_text(encoding="utf-8"))["servers"]["db"]
    assert v["type"] == "stdio" and v["args"][-3:] == ["--", "uvx", "mcp-db"]

    connect.main(["guard", "--agent", "all", "--url", URL, "--key", KEY, "--yes"])     # running it twice changes nothing
    assert json.loads(cursor.read_text(encoding="utf-8")) == g

    connect.main(["guard", "--agent", "all", "--remove", "--yes"])
    assert json.loads(cursor.read_text(encoding="utf-8")) == original
    assert json.loads(vscode.read_text(encoding="utf-8")) == {"servers": {"db": {"type": "stdio", "command": "uvx", "args": ["mcp-db"]}}}
    assert list(cursor.parent.glob("mcp.json.bak-*"))                                     # backups kept


def test_guard_with_no_configs(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    connect.main(["guard", "--agent", "all", "--url", URL, "--key", KEY, "--yes"])
    assert "No MCP configs found" in capsys.readouterr().out


def test_connect_all_and_undo(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(connect.shutil, "which", lambda name: None)       # don't call a real `claude` / `codex`
    for d in (".claude", ".cursor", ".codex"):
        (home / d).mkdir()
    mcp = home / ".cursor" / "mcp.json"
    original = {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}}}
    mcp.write_text(json.dumps(original), encoding="utf-8")

    connect.main(["all", "--url", URL, "--key", KEY, "--yes"])
    claude = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert KEY in json.dumps(claude["hooks"]["PreToolUse"])
    assert "agent_hook.py" in (home / ".cursor" / "hooks.json").read_text(encoding="utf-8")
    assert "agent_hook.py" in (home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    assert not (home / ".gemini").exists()                                 # not installed: left alone
    assert json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]["github"]["args"][0] == str(connect.PROXY)

    connect.main(["all", "--remove", "--yes"])
    assert json.loads(mcp.read_text(encoding="utf-8")) == original
    assert "hooks" not in json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert not (home / ".codex" / "hooks.json").exists()
