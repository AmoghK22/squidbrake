"""
Python client for Squidbrake. Drop this file into any agent / service.

    from client import Gateway, Denied

    gw = Gateway("https://gateway.example.com", api_key="...", source="my-agent", session_id=run_id)

    @gw.guard()                       # every call is checked + recorded; raises Denied if blocked
    def shell_exec(command: str) -> str:
        ...

    @gw.guard(name="web.fetch")       # works on async functions too
    async def fetch(url: str) -> str:
        ...

    gw.log("deploy", input={"env": "prod"}, output="ok", kind="action")   # record-only

Calls matched by a `review` rule block inside check() / the decorator until a human approves
(the tool then runs) or rejects / the approval times out (Denied is raised).
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import time
from typing import Any, Callable

import httpx

log = logging.getLogger("gateway.client")
LONG_POLL = 25.0  # seconds per /decision request while waiting for a human


class Denied(PermissionError):
    def __init__(self, name: str, reason: str, event_id: str | None, decided_by: str | None = None):
        by = f" (rejected by {decided_by})" if decided_by and decided_by != "timeout" else \
             " (approval timed out)" if decided_by == "timeout" else ""
        super().__init__(f"Gateway denied '{name}': {reason}{by}")
        self.name, self.reason, self.event_id, self.decided_by = name, reason, event_id, decided_by


class GatewayUnavailable(RuntimeError):
    pass


def _log_pending(name: str, decision: dict) -> None:
    log.warning("'%s' is waiting for human approval (event %s, expires %s): %s",
                name, decision["event_id"], decision.get("approval_deadline"), decision["reason"])


class Gateway:
    def __init__(self, url: str | None = None, api_key: str | None = None, *, source: str | None = None,
                 session_id: str | None = None, fail_open: bool = False, timeout: float = 5.0,
                 on_approval_pending: Callable[[str, dict], None] | None = _log_pending):
        """fail_open=False (default): if the gateway is unreachable, tools do NOT run.
        fail_open=True: tools run anyway and the call goes unrecorded. (A call already held for
        approval never runs without a decision, even with fail_open.)
        on_approval_pending(name, decision) is called once when a call is held for a human."""
        self.url = (url or os.environ["GATEWAY_URL"]).rstrip("/")
        self.source, self.session_id, self.fail_open = source, session_id, fail_open
        self.on_approval_pending = on_approval_pending
        headers = {"X-Gateway-Key": api_key or os.getenv("GATEWAY_API_KEY", "")}
        self._http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout)
        self._ahttp = httpx.AsyncClient(base_url=self.url, headers=headers, timeout=timeout)

    # ---- payload helpers
    def _event(self, name, input, kind, metadata, **extra) -> dict:
        return {"name": name, "kind": kind, "input": input, "source": self.source,
                "session_id": self.session_id, "metadata": metadata or {}, **extra}

    def _first(self, name: str, resp: httpx.Response | None) -> dict | None:
        if resp is None:
            if self.fail_open:
                return None
            raise GatewayUnavailable(f"gateway unreachable, refusing to run '{name}'")
        resp.raise_for_status()
        d = resp.json()
        if d["decision"] == "review" and self.on_approval_pending:
            self.on_approval_pending(name, d)
        return d

    @staticmethod
    def _final(name: str, d: dict | None) -> str | None:
        if d is None:
            return None
        if d["decision"] != "allow":
            raise Denied(name, d.get("decision_note") or d["reason"], d["event_id"], d.get("decided_by"))
        return d["event_id"]

    @staticmethod
    def _gave_up(d: dict) -> bool:
        # The server decides by the deadline; allow generous slack for clock skew / outages.
        from datetime import datetime, timezone
        deadline = datetime.fromisoformat(d["approval_deadline"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - deadline).total_seconds() > 60

    # ---- sync API
    def check(self, name: str, input: Any = None, *, kind: str = "tool_call", metadata: dict | None = None) -> str | None:
        """Ask before running. Returns event_id; raises Denied if blocked. Waits while a human reviews it."""
        try:
            resp = self._http.post("/v1/events", json=self._event(name, input, kind, metadata))
        except httpx.TransportError:
            resp = None
        d = self._first(name, resp)
        while d is not None and d["decision"] == "review":
            try:
                r = self._http.get(f"/v1/events/{d['event_id']}/decision",
                                   params={"wait": LONG_POLL}, timeout=LONG_POLL + 10)
                r.raise_for_status()
                d = r.json()
            except httpx.TransportError:
                if self._gave_up(d):
                    raise GatewayUnavailable(f"lost the gateway while '{name}' awaited approval")
                time.sleep(2)
        return self._final(name, d)

    def result(self, event_id: str | None, *, output: Any = None, error: str | None = None,
               duration_ms: float | None = None) -> None:
        if event_id is None:
            return
        try:
            self._http.post(f"/v1/events/{event_id}/result",
                            json={"output": output, "error": error, "duration_ms": duration_ms})
        except httpx.TransportError:
            pass  # the call already ran; don't fail the tool because reporting failed

    def log(self, name: str, *, input: Any = None, output: Any = None, error: str | None = None,
            kind: str = "action", duration_ms: float | None = None, metadata: dict | None = None) -> dict:
        """Record an action that already happened (single call)."""
        return self._http.post("/v1/events", json=self._event(
            name, input, kind, metadata, output=output, error=error, duration_ms=duration_ms)).json()

    # ---- async API
    async def acheck(self, name: str, input: Any = None, *, kind: str = "tool_call", metadata: dict | None = None) -> str | None:
        try:
            resp = await self._ahttp.post("/v1/events", json=self._event(name, input, kind, metadata))
        except httpx.TransportError:
            resp = None
        d = self._first(name, resp)
        while d is not None and d["decision"] == "review":
            try:
                r = await self._ahttp.get(f"/v1/events/{d['event_id']}/decision",
                                          params={"wait": LONG_POLL}, timeout=LONG_POLL + 10)
                r.raise_for_status()
                d = r.json()
            except httpx.TransportError:
                if self._gave_up(d):
                    raise GatewayUnavailable(f"lost the gateway while '{name}' awaited approval")
                await asyncio.sleep(2)
        return self._final(name, d)

    async def aresult(self, event_id: str | None, *, output: Any = None, error: str | None = None,
                      duration_ms: float | None = None) -> None:
        if event_id is None:
            return
        try:
            await self._ahttp.post(f"/v1/events/{event_id}/result",
                                   json={"output": output, "error": error, "duration_ms": duration_ms})
        except httpx.TransportError:
            pass

    # ---- decorator
    def guard(self, name: str | None = None, *, kind: str = "tool_call"):
        def deco(fn: Callable):
            tool = name or fn.__name__
            sig = inspect.signature(fn)

            def args_of(a, kw):
                bound = sig.bind_partial(*a, **kw)
                return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def awrapper(*a, **kw):
                    eid = await self.acheck(tool, args_of(a, kw), kind=kind)
                    t0 = time.perf_counter()
                    try:
                        out = await fn(*a, **kw)
                    except Exception as e:
                        await self.aresult(eid, error=f"{type(e).__name__}: {e}", duration_ms=(time.perf_counter() - t0) * 1000)
                        raise
                    await self.aresult(eid, output=out, duration_ms=(time.perf_counter() - t0) * 1000)
                    return out
                return awrapper

            @functools.wraps(fn)
            def wrapper(*a, **kw):
                eid = self.check(tool, args_of(a, kw), kind=kind)
                t0 = time.perf_counter()
                try:
                    out = fn(*a, **kw)
                except Exception as e:
                    self.result(eid, error=f"{type(e).__name__}: {e}", duration_ms=(time.perf_counter() - t0) * 1000)
                    raise
                self.result(eid, output=out, duration_ms=(time.perf_counter() - t0) * 1000)
                return out
            return wrapper
        return deco
