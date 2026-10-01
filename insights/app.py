"""
Squidbrake Insights: see how pilot users use Squidbrake, without ever seeing what their agents did.

  /start/<code>        the page you send a founder: their install commands, with their pilot code filled in
  /install.ps1 | .sh   one-line installers (pipx + squidbrake)
  /admin               your dashboard (INSIGHTS_ADMIN_KEY): pilots, installs, activity, what got blocked
  POST /v1/pilot/join  an install joins with a code (squidbrake pilot join)
  POST /v1/ping        an install's usage counts (every 6 hours)

Run:  INSIGHTS_ADMIN_KEY=... uvicorn app:app --port 8090      (data in INSIGHTS_DB, default ./data/insights.db)
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
DB = Path(os.getenv("INSIGHTS_DB", HERE / "data" / "insights.db"))
ADMIN_KEY = os.getenv("INSIGHTS_ADMIN_KEY", "")
PUBLIC_URL = os.getenv("INSIGHTS_PUBLIC_URL", "").rstrip("/")
CONTACT = os.getenv("INSIGHTS_CONTACT", "")           # shown on start pages, e.g. "WhatsApp +91..., you@x.com"
CODE_RE = re.compile(r"^[a-z0-9-]{3,40}$")

app = FastAPI(title="Squidbrake Insights", docs_url=None, redoc_url=None)
_lock = threading.Lock()


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


with db() as _c:
    _c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS pilots (code TEXT PRIMARY KEY, company TEXT NOT NULL, contact TEXT, note TEXT,
        created_at TEXT NOT NULL, page_views INTEGER NOT NULL DEFAULT 0, first_view TEXT, last_view TEXT);
    CREATE TABLE IF NOT EXISTS installs (install_id TEXT PRIMARY KEY, code TEXT NOT NULL, joined_at TEXT NOT NULL,
        left_at TEXT, last_seen TEXT, version TEXT, os TEXT, mode TEXT, rules INTEGER, agents TEXT,
        rules_hit TEXT, total_events INTEGER, first_event TEXT);
    CREATE TABLE IF NOT EXISTS days (install_id TEXT NOT NULL, day TEXT NOT NULL, counts TEXT NOT NULL,
        PRIMARY KEY (install_id, day));
    """)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def admin(x_admin_key: str = Header(default="")) -> None:
    if not ADMIN_KEY or not hmac.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(401, "admin key required")


def public_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


# --------------------------------------------------------------------------- installs report in

class JoinIn(BaseModel):
    code: str = Field(max_length=40)
    install_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: str = Field(default="", max_length=40)
    os: str = Field(default="", max_length=80)


class PingIn(BaseModel):
    code: str = Field(max_length=40)
    install_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    usage: dict


def _pilot(c, code: str):
    row = c.execute("SELECT * FROM pilots WHERE code=?", (code.lower(),)).fetchone()
    if not row:
        raise HTTPException(404, "unknown pilot code: check the link you were sent")
    return row


@app.post("/v1/pilot/join")
def join(j: JoinIn):
    with _lock, db() as c:
        p = _pilot(c, j.code)
        c.execute("""INSERT INTO installs (install_id, code, joined_at, version, os) VALUES (?,?,?,?,?)
                     ON CONFLICT(install_id) DO UPDATE SET code=excluded.code, left_at=NULL, version=excluded.version,
                     os=excluded.os""", (j.install_id, p["code"], now(), j.version, j.os))
    return {"company": p["company"]}


@app.post("/v1/pilot/leave")
def leave(j: PingIn | JoinIn):
    with _lock, db() as c:
        c.execute("UPDATE installs SET left_at=? WHERE install_id=?", (now(), j.install_id))
    return {"ok": True}


def _int(v) -> int:
    try:
        return max(0, min(int(v), 10 ** 9))
    except (TypeError, ValueError):
        return 0


@app.post("/v1/ping")
async def ping(request: Request):
    raw = await request.body()
    if len(raw) > 64_000:
        raise HTTPException(413, "too large")
    p = PingIn.model_validate_json(raw)
    u = p.usage
    days = {d: {k: _int(v) for k, v in (cnt or {}).items()} for d, cnt in (u.get("days") or {}).items()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d))}
    clip = lambda m, n=30: json.dumps({str(k)[:60]: _int(v) for k, v in list((m or {}).items())[:n]})
    with _lock, db() as c:
        _pilot(c, p.code)
        if not c.execute("SELECT 1 FROM installs WHERE install_id=? AND left_at IS NULL", (p.install_id,)).fetchone():
            raise HTTPException(403, "this install hasn't joined (or has left) the pilot")
        c.execute("""UPDATE installs SET last_seen=?, version=?, os=?, mode=?, rules=?, agents=?, rules_hit=?,
                     total_events=?, first_event=? WHERE install_id=?""",
                  (now(), str(u.get("version", ""))[:40], str(u.get("os", ""))[:80], str(u.get("mode", ""))[:20],
                   _int(u.get("rules")), clip(u.get("agents")), clip(u.get("rules_hit")), _int(u.get("total_events")),
                   str(u.get("first_event", ""))[:10], p.install_id))
        for d, cnt in days.items():
            c.execute("INSERT OR REPLACE INTO days (install_id, day, counts) VALUES (?,?,?)", (p.install_id, d, json.dumps(cnt)))
    return {"ok": True}


# --------------------------------------------------------------------------- admin

class PilotIn(BaseModel):
    company: str = Field(min_length=1, max_length=80)
    contact: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=500)


@app.post("/v1/admin/pilots", dependencies=[Depends(admin)])
def create_pilot(p: PilotIn, request: Request):
    slug = re.sub(r"[^a-z0-9]+", "-", p.company.lower()).strip("-")[:20] or "pilot"
    code = f"{slug}-{secrets.token_hex(3)}"
    with _lock, db() as c:
        c.execute("INSERT INTO pilots (code, company, contact, note, created_at) VALUES (?,?,?,?,?)",
                  (code, p.company, p.contact, p.note, now()))
    return {"code": code, "link": f"{public_url(request)}/start/{code}"}


@app.delete("/v1/admin/pilots/{code}", dependencies=[Depends(admin)])
def delete_pilot(code: str):
    with _lock, db() as c:
        ids = [r[0] for r in c.execute("SELECT install_id FROM installs WHERE code=?", (code,))]
        c.executemany("DELETE FROM days WHERE install_id=?", [(i,) for i in ids])
        c.execute("DELETE FROM installs WHERE code=?", (code,))
        c.execute("DELETE FROM pilots WHERE code=?", (code,))
    return {"deleted": code}


SUMS = ("events", "allowed", "held", "approved", "rejected", "blocked", "timed_out", "failed", "would_block", "would_hold")


@app.get("/v1/admin/overview", dependencies=[Depends(admin)])
def overview(request: Request):
    today = datetime.now(timezone.utc).date()
    span = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    with db() as c:
        pilots = [dict(r) for r in c.execute("SELECT * FROM pilots ORDER BY created_at DESC")]
        installs = [dict(r) for r in c.execute("SELECT * FROM installs")]
        days = c.execute("SELECT install_id, day, counts FROM days WHERE day >= ?", (span[0],)).fetchall()
    by_install: dict[str, dict] = {}
    for iid, day, counts in days:
        by_install.setdefault(iid, {})[day] = json.loads(counts)
    t = datetime.now(timezone.utc)
    seen_within = lambda s, h: bool(s) and t - datetime.fromisoformat(s) < timedelta(hours=h)
    out, totals = [], {k: 0 for k in SUMS}
    for p in pilots:
        mine = [i for i in installs if i["code"] == p["code"]]
        daily = {d: {k: 0 for k in SUMS} for d in span}
        agents, hits = {}, {}
        for i in mine:
            for d, cnt in by_install.get(i["install_id"], {}).items():
                if d in daily:
                    for k in SUMS: daily[d][k] += cnt.get(k, 0)
            for k, v in json.loads(i["agents"] or "{}").items(): agents[k] = agents.get(k, 0) + v
            for k, v in json.loads(i["rules_hit"] or "{}").items(): hits[k] = hits.get(k, 0) + v
        week = {k: sum(daily[d][k] for d in span[-7:]) for k in SUMS}
        for k in SUMS: totals[k] += week[k]
        active = [i for i in mine if not i["left_at"]]
        last = max((i["last_seen"] or "" for i in active), default="")
        stage = ("left" if mine and not active else "active" if seen_within(last, 48) and week["events"] else
                 "quiet" if last else "installed" if active else "opened link" if p["page_views"] else "link sent")
        out.append({**p, "link": f"{public_url(request)}/start/{p['code']}", "stage": stage, "installs": len(active),
                    "last_seen": last or None, "versions": sorted({i["version"] for i in active if i["version"]}),
                    "modes": sorted({i["mode"] for i in active if i["mode"]}), "agents": agents,
                    "rules_hit": dict(sorted(hits.items(), key=lambda kv: -kv[1])[:6]), "week": week,
                    "daily": [daily[d]["events"] for d in span], "total_events": sum(i["total_events"] or 0 for i in mine)})
    return {"span": span, "pilots": out, "week": totals,
            "active_pilots": sum(1 for p in out if p["stage"] == "active"),
            "installs": sum(p["installs"] for p in out)}


# --------------------------------------------------------------------------- pages

PAGE = lambda name: (HERE / name).read_text(encoding="utf-8")


@app.get("/start/{code}", response_class=HTMLResponse)
def start_page(code: str, request: Request):
    if not CODE_RE.match(code):
        raise HTTPException(404)
    with _lock, db() as c:
        p = c.execute("SELECT * FROM pilots WHERE code=?", (code,)).fetchone()
        if not p:
            return HTMLResponse(PAGE("start.html").replace("__DATA__", json.dumps({"missing": True})), status_code=404)
        c.execute("UPDATE pilots SET page_views=page_views+1, first_view=COALESCE(first_view, ?), last_view=? WHERE code=?",
                  (now(), now(), code))
    data = {"company": p["company"], "code": code, "server": public_url(request), "contact": CONTACT}
    return HTMLResponse(PAGE("start.html").replace("__DATA__", json.dumps(data).replace("</", "<\\/")))


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(PAGE("admin.html"))


@app.get("/install.ps1", response_class=PlainTextResponse)
def install_ps1():
    return PlainTextResponse(PAGE("install.ps1"), media_type="text/plain; charset=utf-8")


@app.get("/install.sh", response_class=PlainTextResponse)
def install_sh():
    return PlainTextResponse(PAGE("install.sh"), media_type="text/plain; charset=utf-8")


@app.get("/health")
def health():
    return {"ok": True}
