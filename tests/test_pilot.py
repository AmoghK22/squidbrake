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
os.environ["HOSTED_DOMAIN"] = "app.example.com"
os.environ["HOSTED_MAX"] = "2"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import pilot  # noqa: E402

SECRET = "PLANTED-MARKER-not-a-real-secret"   # must never show up in what a pilot install sends


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


def test_hosted_pilot_lifecycle(insights):
    admin = {"X-Admin-Key": "admin-test-key"}
    r = insights.post("/v1/admin/pilots", headers=admin, json={"company": "Hosted Co", "hosted": True}).json()
    code, dash = r["code"], r["dashboard"]
    assert dash == "https://hosted-co.app.example.com"
    # Caddy may only get certificates for hosted pilots that exist
    assert insights.get("/v1/caddy/ask", params={"domain": "hosted-co.app.example.com"}).status_code == 200
    assert insights.get("/v1/caddy/ask", params={"domain": "evil.app.example.com"}).status_code == 404
    assert insights.get("/v1/caddy/ask", params={"domain": "hosted-co.app.example.com.attacker.io"}).status_code == 404
    # the provisioner sees it, starts it and reports the keys; the keys are never in the admin overview
    q = insights.get("/v1/admin/provision", headers=admin).json()["pilots"]
    assert any(p["code"] == code and p["state"] == "requested" for p in q)
    assert insights.get("/v1/admin/provision").status_code == 401
    assert insights.post("/v1/admin/provisioned", json={"code": code, "state": "running"}).status_code == 401
    insights.post("/v1/admin/provisioned", headers=admin, json={"code": code, "state": "running",
                                                                "admin_key": "gw_admin_xyz", "agent_key": "gw_agent_xyz"})
    overview = insights.get("/v1/admin/overview", headers=admin).text
    assert "gw_admin_xyz" not in overview and "gw_agent_xyz" not in overview
    # the founder's page shows them once
    k = insights.post(f"/v1/pilot/{code}/keys").json()
    assert k["admin_key"] == "gw_admin_xyz" and k["agent_key"] == "gw_agent_xyz" and k["dashboard"] == dash
    assert insights.post(f"/v1/pilot/{code}/keys").status_code == 409
    # slots are limited (HOSTED_MAX=2 here)
    insights.post("/v1/admin/pilots", headers=admin, json={"company": "Second", "hosted": True})
    assert insights.post("/v1/admin/pilots", headers=admin, json={"company": "Third", "hosted": True}).status_code == 409
    # deleting waits for the provisioner to remove the gateway
    assert insights.delete(f"/v1/admin/pilots/{code}", headers=admin).json() == {"deleting": code}
    assert insights.get("/v1/caddy/ask", params={"domain": "hosted-co.app.example.com"}).status_code == 404
    insights.post("/v1/admin/provisioned", headers=admin, json={"code": code, "state": "deleted"})
    assert all(p["code"] != code for p in insights.get("/v1/admin/overview", headers=admin).json()["pilots"])


def test_pilot_server_must_be_https_or_internal(tmp_path):
    for bad in ("http://pilots.example.com", "ftp://x", ""):
        assert pilot.join(tmp_path, "c", bad, True, "1") == 2

def test_admin_password_and_sessions(insights):
    key = {"X-Admin-Key": "admin-test-key"}
    assert insights.post("/v1/admin/login", json={"password": "nope"}).status_code == 401
    # the server key signs in the first time; then a password is set with it
    token = insights.post("/v1/admin/login", json={"password": "admin-test-key"}).json()["token"]
    s = {"X-Admin-Key": token}
    assert insights.get("/v1/admin/me", headers=s).json() == {"has_password": False}
    assert insights.post("/v1/admin/password", headers=s, json={"current": "wrong", "new": "a-long-password-1"}).status_code == 403
    assert insights.post("/v1/admin/password", headers=s, json={"current": "admin-test-key", "new": "short"}).status_code == 422
    assert insights.post("/v1/admin/password", headers=s, json={"current": "admin-test-key", "new": "a-long-password-1"}).status_code == 200
    assert insights.get("/v1/admin/overview", headers=s).status_code == 401          # changing it signs everyone out
    s = {"X-Admin-Key": insights.post("/v1/admin/login", json={"password": "a-long-password-1"}).json()["token"]}
    assert insights.get("/v1/admin/me", headers=s).json() == {"has_password": True}
    import app as insights_app
    with insights_app.db() as c:                                                          # stored hashed, never as text
        stored = c.execute("SELECT value FROM settings WHERE key='admin_password'").fetchone()[0]
    assert "a-long-password-1" not in stored and stored.startswith("pbkdf2_sha256$")
    insights.post("/v1/admin/logout", headers=s)
    assert insights.get("/v1/admin/overview", headers=s).status_code == 401
    assert insights.get("/v1/admin/overview", headers=key).status_code == 200            # the server key still works (provisioner)
    insights_app._failures.clear()
    for _ in range(8):
        insights.post("/v1/admin/login", json={"password": "guess"})
    assert insights.post("/v1/admin/login", json={"password": "a-long-password-1"}).status_code == 429   # slowed down
    insights_app._failures.clear()