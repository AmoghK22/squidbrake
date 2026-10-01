"""
Pilot programme: share usage COUNTS with the Squidbrake team, only after you join with a code you were given.

    squidbrake pilot join CODE --server URL    start sharing (shows exactly what is sent, and asks first)
    squidbrake pilot status                    what is shared, where, and when it was last sent
    squidbrake pilot leave                     stop sharing

Nothing is sent unless you join. What is sent, at start and every 6 hours:
  - the Squidbrake version, operating system, enforce/shadow mode and number of rules
  - the agents connected, by their labels (e.g. "claude-code", "antigravity")
  - per day, for the last 7 days: how many actions were allowed, held, approved, rejected, blocked, timed out or failed
  - which rules blocked or held things (rule ids such as "command:catastrophic_command")
Never sent: commands, file contents, prompts, tool inputs or outputs, keys, names of people.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import case, func, select

log = logging.getLogger("gateway")
INTERVAL = 6 * 3600
WHAT_IS_SENT = __doc__.split("Nothing is sent unless you join.")[1].strip()


def _path(home: Path) -> Path:
    return home / "pilot.json"


def load(home: Path) -> dict | None:
    try:
        return json.loads(_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(home: Path, cfg: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    _path(home).write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def usage(engine, events, mode: str, rules: int, version: str) -> dict:
    """Counts only. No inputs, outputs, names of people or anything an agent did beyond its outcome."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    day = func.substr(events.c.created_at, 1, 10)
    human = events.c.decided_by.isnot(None) & (events.c.decided_by != "timeout")
    n = lambda cond: func.sum(case((cond, 1), else_=0))
    in_week = events.c.created_at >= since
    with engine.connect() as conn:
        days = {d: {"events": int(t or 0), "allowed": int(a or 0), "held": int(h or 0), "approved": int(ap or 0),
                    "rejected": int(rj or 0), "blocked": int(b or 0), "timed_out": int(to or 0), "failed": int(f or 0),
                    "would_block": int(wb or 0), "would_hold": int(wh or 0)}
                for d, t, a, h, ap, rj, b, to, f, wb, wh in conn.execute(select(
                    day, func.count(),
                    n((events.c.decision == "allow") & events.c.decided_by.is_(None)),
                    n(events.c.approval_deadline.isnot(None)),
                    n(human & (events.c.decision == "allow")),
                    n(human & (events.c.decision == "deny")),
                    n((events.c.status == "denied") & events.c.decided_by.is_(None)),
                    n(events.c.decided_by == "timeout"),
                    n(events.c.status == "failed"),
                    n(events.c.would == "deny"),
                    n(events.c.would == "review"),
                ).where(in_week).group_by(day)).all()}
        agent = func.coalesce(events.c.source, events.c.client)
        agents = dict(conn.execute(select(agent, func.count()).where(in_week).group_by(agent)).all())
        rules_hit = dict(conn.execute(select(events.c.rule_id, func.count()).where(
            in_week, events.c.rule_id.isnot(None),
            (events.c.status == "denied") | events.c.approval_deadline.isnot(None),
        ).group_by(events.c.rule_id).order_by(func.count().desc()).limit(15)).all())
        total, first = conn.execute(select(func.count(), func.min(events.c.created_at)).select_from(events)).one()
    return {"version": version, "os": f"{platform.system()} {platform.release()}", "python": platform.python_version(),
            "mode": mode, "rules": rules, "agents": {str(k): v for k, v in agents.items() if k},
            "days": days, "rules_hit": rules_hit, "total_events": total, "first_event": (first or "")[:10]}


def _post(server: str, path: str, body: dict) -> dict:
    r = httpx.post(server.rstrip("/") + path, json=body, timeout=15)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise RuntimeError(f"{r.status_code}: {detail}")
    return r.json()


def send(home: Path, payload: dict) -> bool:
    cfg = load(home)
    if not cfg:
        return False
    try:
        _post(cfg["server"], "/v1/ping", {"code": cfg["code"], "install_id": cfg["install_id"], "usage": payload})
    except (httpx.HTTPError, RuntimeError) as e:
        log.info("pilot: usage not sent (%s); will try again later", e)
        return False
    cfg["last_sent"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save(home, cfg)
    return True


async def loop(home: Path, make_payload) -> None:
    """Runs inside the gateway. Does nothing at all unless this install joined a pilot."""
    await asyncio.sleep(20)
    while True:
        if load(home):
            try:
                await asyncio.to_thread(lambda: send(home, make_payload()))
            except Exception as e:  # never let reporting affect the gateway
                log.info("pilot: %s", e)
        await asyncio.sleep(INTERVAL)


# --------------------------------------------------------------------------- command line

def join(home: Path, code: str, server: str | None, yes: bool, version: str) -> int:
    server = (server or os.getenv("SQUIDBRAKE_PILOT_SERVER") or "").rstrip("/")
    if not server.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        print("Give the pilot server you were sent, e.g. --server https://...  (https only)", file=sys.stderr)
        return 2
    print(f"\nJoining the Squidbrake pilot with code {code}.\nThis sends usage counts to {server}:\n")
    print("  " + WHAT_IS_SENT.replace("\n", "\n  ") + "\n")
    if not yes and input("Share these counts? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Not joined. Nothing is shared.")
        return 1
    cfg = load(home) or {}
    install_id = cfg.get("install_id") or uuid.uuid4().hex
    try:
        r = _post(server, "/v1/pilot/join", {"code": code, "install_id": install_id, "version": version,
                                             "os": f"{platform.system()} {platform.release()}"})
    except (httpx.HTTPError, RuntimeError) as e:
        print(f"Couldn't join: {e}", file=sys.stderr)
        return 1
    _save(home, {"code": code, "server": server, "install_id": install_id, "company": r.get("company"),
                 "joined": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    print(f"Joined{(' as ' + r['company']) if r.get('company') else ''}. Thank you!"
          f"\nCounts are sent while the gateway runs (restart it if it's running). Stop anytime: squidbrake pilot leave")
    return 0


def leave(home: Path) -> int:
    cfg = load(home)
    if not cfg:
        print("Not in a pilot; nothing is shared.")
        return 0
    try:
        _post(cfg["server"], "/v1/pilot/leave", {"code": cfg["code"], "install_id": cfg["install_id"]})
    except (httpx.HTTPError, RuntimeError):
        pass
    _path(home).unlink(missing_ok=True)
    print("Left the pilot. Nothing more is shared.")
    return 0


def status(home: Path) -> int:
    cfg = load(home)
    if not cfg:
        print("Not in a pilot; nothing is shared.")
        return 0
    print(f"In the pilot {cfg.get('company') or cfg['code']} (code {cfg['code']}), sharing usage counts with {cfg['server']}.")
    print(f"Last sent: {cfg.get('last_sent') or 'not yet (sent while the gateway runs)'}.  Stop: squidbrake pilot leave")
    return 0
