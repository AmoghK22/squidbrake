"""
Where is this action sending things, and where did that address come from?

Prompt-injection attacks that actually happened (GitHub MCP, Supabase MCP, EchoLeak, tool poisoning) share one shape:
the agent reads content someone else wrote (a web page, an email, an issue, a ticket), that content says "send X to
<attacker>", and the agent does. No model is needed to catch the core of it: if an action sends data to a destination
(an email address, a URL, a bank account, a repo) that appears in untrusted content, and NOT in what the user asked or
in the company's own systems, the destination came from the untrusted content.

This module is pure (no database): it finds destinations in an action's input and checks where a value appears.
server.py decides what counts as untrusted, loads the texts, and applies the effects from rules.yaml.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

# Keys whose values say WHERE something goes.
DEST_KEYS = {
    "to", "cc", "bcc", "recipient", "recipients", "email", "emails", "address", "reply_to", "forward_to",
    "url", "uri", "endpoint", "webhook", "webhook_url", "callback", "callback_url", "host", "domain", "target_url",
    "to_account", "account", "account_number", "iban", "destination", "payee", "beneficiary",
    "channel", "repo", "repository", "owner", "remote",
}
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
URL_RE = re.compile(r"\bhttps?://[^\s\"'<>)]+", re.I)
SCP_RE = re.compile(r"\b[\w.-]+@([\w.-]+\.[A-Za-z]{2,}):")               # scp / rsync / git over ssh: user@host:
# Shell commands that send data somewhere.
UPLOAD_RE = re.compile(r"\b(curl|wget|http|https|xh|nc|ncat|netcat|scp|rsync|sftp|ftp|invoke-webrequest|iwr|"
                       r"invoke-restmethod|irm|git\s+push|gh\s+(gist|issue|pr|api|release))\b", re.I)
CURL_SEND_RE = re.compile(r"(^|\s)(-d|--data\S*|-F|--form|-T|--upload-file|-X\s*(POST|PUT|PATCH)|--json|"
                          r"--post-data|--post-file|--body-file|-Method\s+(Post|Put))(\s|=|$)", re.I)


def _walk(value: Any, key: str = "") -> list[tuple[str, str]]:
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            out += _walk(v, str(k).lower())
    elif isinstance(value, list):
        for v in value:
            out += _walk(v, key)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        out.append((key, str(value)))
    return out


def destinations(input: Any, command: str | None = None) -> list[dict]:
    """[{field, value, kind}] for where this action sends things. kind: email | host | account | name."""
    found: list[dict] = []
    seen = set()

    def add(field: str, value: str, kind: str) -> None:
        v = value.strip().strip(".,;").lower() if kind in ("email", "host") else re.sub(r"\s+", "", value.strip())
        if len(v) >= 3 and (kind, v) not in seen:
            seen.add((kind, v))
            found.append({"field": field, "value": v, "kind": kind})

    for key, text in _walk(input):
        if key not in DEST_KEYS or not text.strip():
            continue
        emails, urls = EMAIL_RE.findall(text), URL_RE.findall(text)
        for e in emails:
            add(key, e, "email")
        for u in urls:
            host = urlsplit(u).hostname
            if host:
                add(key, host, "host")
        if not emails and not urls:
            if key in ("to_account", "account", "account_number", "iban", "payee", "beneficiary", "destination"):
                add(key, text, "account")
            elif key in ("host", "domain"):
                add(key, text, "host")
            elif key in ("repo", "repository", "channel", "owner", "remote") and len(text) <= 100:
                add(key, text, "name")
    if command and UPLOAD_RE.search(command):
        sends = CURL_SEND_RE.search(command) or re.search(r"\b(scp|rsync|sftp|nc|ncat|netcat|git\s+push|gh\s)", command, re.I)
        if sends:
            for u in URL_RE.findall(command):
                host = urlsplit(u).hostname
                if host:
                    add("command", host, "host")
            for host in SCP_RE.findall(command):
                add("command", host, "host")
            for e in EMAIL_RE.findall(command):
                if not SCP_RE.search(command):
                    add("command", e, "email")
    return found


def sends_out(command: str | None) -> bool:
    """Does this shell command send data somewhere (not just download)?"""
    return bool(command and UPLOAD_RE.search(command)
                and (CURL_SEND_RE.search(command) or re.search(r"\b(scp|rsync|sftp|nc|ncat|netcat|git\s+push)\b", command, re.I)))


def appears_in(dest: dict, text: str) -> bool:
    """Does this destination appear in a piece of text? Hosts also match as part of URLs and email domains."""
    if not text:
        return False
    low = text.lower()
    v = dest["value"]
    if dest["kind"] == "account":
        return v.lower() in re.sub(r"\s+", "", low)
    if dest["kind"] == "host":
        return re.search(rf"(?<![\w.-]){re.escape(v)}(?![\w-])", low) is not None
    return v in low


def own_domain(dest: dict, company_domains: list[str]) -> bool:
    host = dest["value"].split("@")[-1] if dest["kind"] in ("email", "host") else ""
    return bool(host) and any(host == d or host.endswith("." + d) for d in company_domains)
