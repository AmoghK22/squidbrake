"""Pilot usage sharing: nothing without joining, only counts when joined, and the insights service guards its doors."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "insights"))
if "server" not in sys.modules:   # run on its own: a throwaway database (test_server.py sets the same keys)
    _tmp = Path(tempfile.mkdtemp())
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_tmp / 'gw.db').as_posix()}")
    os.environ.setdefault("RULES_PATH", str(_tmp / "rules.yaml"))
    os.environ.setdefault("GATEWAY_API_KEYS", "tester:k1,boss:k2,boss2:k3")
    os.environ.setdefault("GATEWAY_APPROVERS", "boss,boss2")
os.environ["INSIGHTS_DB"] = str(Path(tempfile.mkdtemp()) / "insights.db")
os.environ["INSIGHTS_ADMIN_KEY"] = "admin-test-key"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import pilot  # noqa: E402

SECRET = "sk_live_DO_NOT_LEAK_7731"


@pytest.fixture(scope="module")
def insights():
    import app as insights_app
    with TestClient(insights_app.app) as c:
        yield c


@pytest.fixture()
def gateway():
    import server
    with TestClient(server.app) as c:
        yield server, c


def test_usage_is_counts_only(gateway):
    server, c = gateway
    c.post("/v1/events", headers={"X-Gateway-Key": "k1"}, json={
        "name": "Bash", "source": "pilot-test", "input": {"command": f"echo {SECRET} > .env"}})
    u = pilot.usage(server.engine, server.events, "enforce", 3, "9.9.9")
    text = json.dumps(u)
    assert SECRET not in text and "echo" not in text and ".env" not in text   # no content, ever
    assert u["agents"].get("pilot-test", 0) >= 1 and u["version"] == "9.9.9"
    assert all(set(day) >= {"events", "held", "blocked", "approved"} for day in u["days"].values())


def test_nothing_is_sent_without_joining(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(a))
    assert pilot.send(tmp_path, {"days": {}}) is False and calls == []


def test_join_ping_leave(insights, tmp_path, monkeypatch):
    admin = {"X-Admin-Key": "admin-test-key"}
    assert insights.post("/v1/admin/pilots", json={"company": "Acme"}).status_code == 401
    code = insights.post("/v1/admin/pilots", headers=admin, json={"company": "Acme"}).json()["code"]
    assert insights.get(f"/start/{code}").status_code == 200 and "Acme" in insights.get(f"/start/{code}").text
    assert insights.get("/start/no-such-code").status_code == 404

    # the gateway side talks to the in-process insights service
    path_of = lambda url: "/" + url.split("://", 1)[1].split("/", 1)[1]          # http://localhost/v1/x -> /v1/x
    monkeypatch.setattr(pilot.httpx, "post", lambda url, json, timeout: insights.post(path_of(url), json=json))
    assert pilot.join(tmp_path, "nope-000000", "http://localhost", True, "1.0") == 1          # unknown code
    assert pilot.join(tmp_path, code, "http://localhost", True, "1.0") == 0
    cfg = pilot.load(tmp_path)
    usage = {"version": "1.0", "agents": {"claude-code": 5}, "rules_hit": {"command:catastrophic_command": 1},
             "days": {"2026-10-01": {"events": 5, "held": 2, "blocked": 1}, "../../etc": {"events": 9}}, "total_events": 5}
    assert pilot.send(tmp_path, usage) is True
    p = next(p for p in insights.get("/v1/admin/overview", headers=admin).json()["pilots"] if p["code"] == code)
    assert p["installs"] == 1 and p["agents"] == {"claude-code": 5} and p["total_events"] == 5

    # pings from an install that never joined (or left) are refused
    stranger = {"code": code, "install_id": "f" * 32, "usage": usage}
    assert insights.post("/v1/ping", json=stranger).status_code == 403
    assert pilot.leave(tmp_path) == 0 and pilot.load(tmp_path) is None
    again = {"code": code, "install_id": cfg["install_id"], "usage": usage}
    assert insights.post("/v1/ping", json=again).status_code == 403
