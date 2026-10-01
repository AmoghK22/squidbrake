"""agent_hook.py speaks each agent's hook format; connect.py agents installs and removes it."""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if "server" not in sys.modules:   # run on its own: a throwaway database (test_server.py sets the same keys)
    _tmp = Path(tempfile.mkdtemp())
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_tmp / 'gw.db').as_posix()}")
    os.environ.setdefault("RULES_PATH", str(_tmp / "rules.yaml"))
    os.environ.setdefault("GATEWAY_API_KEYS", "tester:k1,boss:k2,boss2:k3")
    os.environ.setdefault("GATEWAY_APPROVERS", "boss,boss2")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import agent_hook  # noqa: E402
import connect  # noqa: E402

# what each agent sends for: a home-folder wipe, a harmless listing, reading .env, and an MCP tool
CASES = {
    "cursor": [{"hook_event_name": "beforeShellExecution", "command": "rm -rf build/ ~/", "cwd": "/w", "conversation_id": "c1"},
               {"hook_event_name": "beforeShellExecution", "command": "ls -la", "conversation_id": "c1"},
               {"hook_event_name": "beforeReadFile", "file_path": "/w/.env", "conversation_id": "c1"}, None],
    "gemini-cli": [{"tool_name": "run_shell_command", "tool_input": {"command": "rm -rf build/ ~/"}, "session_id": "g1"},
                   {"tool_name": "run_shell_command", "tool_input": {"command": "ls -la"}, "session_id": "g1"},
                   {"tool_name": "read_file", "tool_input": {"absolute_path": "/w/.env"}, "session_id": "g1"},
                   {"tool_name": "mcp_github_create_issue", "tool_input": {"title": "x"}, "session_id": "g1"}],
    "codex": [{"tool_name": "Bash", "tool_input": {"command": ["bash", "-lc", "rm -rf build/ ~/"]}, "session_id": "x1"},
              {"tool_name": "Bash", "tool_input": {"command": "ls -la"}, "session_id": "x1"}, None,
              {"tool_name": "mcp__github__create_issue", "tool_input": {}, "session_id": "x1"}],
    "vscode": [{"tool_name": "run_in_terminal", "tool_input": {"command": "rm -rf build/ ~/"}, "session_id": "v1"},
               {"tool_name": "run_in_terminal", "tool_input": {"command": "ls -la"}, "session_id": "v1"},
               {"tool_name": "read_file", "tool_input": {"filePath": "/w/.env"}, "session_id": "v1"}, None],
    "antigravity": [{"toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf build/ ~/", "Cwd": "/w"}}, "conversationId": "a1"},
                    {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls -la"}}, "conversationId": "a1"},
                    {"toolCall": {"name": "view_file", "args": {"AbsolutePath": "/w/.env"}}, "conversationId": "a1"}, None],
}


@pytest.fixture()
def run(monkeypatch):
    import server
    monkeypatch.setattr(server, "policy", server.Policy(ROOT / "rules.yaml"))       # the shipped rules
    monkeypatch.setattr(agent_hook, "KEY", "k1")
    monkeypatch.setattr(agent_hook, "MAX_WAIT", 0.0)                               # held -> answer at once in tests
    monkeypatch.setattr(agent_hook.httpx, "Client", lambda base_url, headers, timeout: TestClient(server.app, headers=headers))

    def go(agent, event):
        monkeypatch.setattr(sys, "argv", ["agent_hook.py", agent])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        with pytest.raises(SystemExit):
            agent_hook.main()
        return json.loads(out.getvalue()) if out.getvalue().strip() else {}

    with TestClient(server.app):
        yield go


def allowed(agent, out):
    return {"cursor": lambda o: o.get("permission") == "allow",
            "gemini-cli": lambda o: o.get("decision") == "allow",
            "antigravity": lambda o: o.get("decision") == "allow",
            "codex": lambda o: o == {},
            "vscode": lambda o: o["hookSpecificOutput"]["permissionDecision"] == "allow"}[agent](out)


@pytest.mark.parametrize("agent", list(CASES))
def test_each_agent(run, agent):
    wipe, listing, secret, mcp = CASES[agent]
    out = run(agent, wipe)
    assert not allowed(agent, out) and "home folder" in json.dumps(out)
    assert allowed(agent, run(agent, listing))
    if secret:
        assert not allowed(agent, run(agent, secret))
    if mcp:
        assert allowed(agent, run(agent, mcp))          # MCP tools are left to `connect guard`


def test_install_and_remove_hooks(tmp_path, monkeypatch):
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".cursor" / "hooks.json").write_text(json.dumps(
        {"version": 1, "hooks": {"beforeShellExecution": [{"command": "./their-own-hook.sh"}]}}), encoding="utf-8")
    connect.main(["agents", "--url", "https://x.app.example.com", "--key", "gw_k", "--yes"])
    cur = json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    shell = cur["hooks"]["beforeShellExecution"]
    assert shell[0] == {"command": "./their-own-hook.sh"} and "agent_hook.py" in shell[1]["command"]
    assert shell[1]["failClosed"] is True and "cursor --url https://x.app.example.com --key gw_k" in shell[1]["command"]
    codex = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert "agent_hook.py" in codex["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert not (tmp_path / ".gemini").exists()                           # not installed: left alone
    connect.main(["agents", "--url", "https://x.app.example.com", "--key", "gw_k", "--yes"])    # idempotent
    assert len(json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["beforeShellExecution"]) == 2
    connect.main(["agents", "--remove", "--yes"])
    assert json.loads((tmp_path / ".cursor" / "hooks.json").read_text(encoding="utf-8")) == {
        "version": 1, "hooks": {"beforeShellExecution": [{"command": "./their-own-hook.sh"}]}}
    assert not (tmp_path / ".codex" / "hooks.json").exists()
