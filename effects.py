"""
What a command will change, measured on this machine before it runs, so the approver sees a number, not a guess.

  git push --force      -> the commits on the remote that the push would remove (as of your last fetch)
  git reset --hard      -> the files with uncommitted changes it would throw away, and the commits it would leave
  git clean -f          -> the untracked files it would delete (git's own dry run)
  git checkout/restore  -> the changed files it would overwrite
  rm -r, Remove-Item, rmdir /s, del -> how many files and how much data the paths hold

Used by the hooks (claude_hook.py, agent_hook.py), which run on the developer's machine: the gateway can't see
the repository. Only reads: git commands here are local (no fetch, no network), dry runs, or counts.
Everything is bounded in time and size, and anything that fails is simply left out.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import time
from pathlib import Path

import commands

BUDGET_SECONDS = 2.0        # for all predictions on one command line
WALK_LIMIT = 20000          # files counted under the paths a delete names, at most
DELETE_PROGRAMS = {"rm", "remove-item", "ri", "rmdir", "rd", "del", "erase", "rimraf"}
WINDOWS_FLAG = re.compile(r"/[a-zA-Z]$")


def _git(cwd: str, *args: str, timeout: float = 2.0) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _names(lines: list[str], n: int = 3) -> str:
    shown = ", ".join(lines[:n])
    return shown + (f" and {len(lines) - n} more" if len(lines) > n else "")


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (f"{n:.0f} byte" + ("" if n == 1 else "s")) if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------- git

def _push(words: list[str], cwd: str) -> str | None:
    args = words[2:]
    force = any(a in ("-f", "--force") or a.startswith("--force-with-lease") or a.startswith("+") for a in args)
    if not force:
        return None
    plain = [a.lstrip("+") for a in args if not a.startswith("-")]
    remote = plain[0] if plain else "origin"
    branch = plain[1].split(":")[-1] if len(plain) > 1 else (_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    local = plain[1].split(":")[0] if len(plain) > 1 and plain[1].split(":")[0] else "HEAD"
    if not branch or branch == "HEAD":
        return None
    target = f"{remote}/{branch}"
    if _git(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{target}") is None:
        return f"{target} isn't known on this machine yet, so nothing on it can be counted"
    lost = (_git(cwd, "log", "--format=%h %s", f"{local}..{target}") or "").splitlines()
    if not lost:
        return f"No commits on {target} would be lost (as of your last fetch)"
    return (f"Removes {len(lost)} commit{'s' if len(lost) != 1 else ''} from {target} that this branch doesn't have "
            f"(as of your last fetch): {_names([l.split(' ', 1)[-1] for l in lost])}")


def _changed_files(cwd: str, paths: list[str] | None = None) -> list[str]:
    out = _git(cwd, "status", "--porcelain", "--untracked-files=no", *(["--", *paths] if paths else []))
    return [l[3:] for l in (out or "").splitlines() if l.strip()]


def _reset(words: list[str], cwd: str) -> str | None:
    if "--hard" not in words:
        return None
    parts = []
    changed = _changed_files(cwd)
    if changed:
        parts.append(f"throws away uncommitted changes in {len(changed)} file{'s' if len(changed) != 1 else ''}: {_names(changed)}")
    ref = next((w for w in words[2:] if not w.startswith("-")), None)
    if ref:
        left = (_git(cwd, "log", "--format=%s", f"{ref}..HEAD") or "").splitlines()
        if left:
            parts.append(f"moves the branch off {len(left)} commit{'s' if len(left) != 1 else ''}: {_names(left)}")
    return ("It " + "; it ".join(parts)) if parts else "No uncommitted changes would be lost"


def _clean(words: list[str], cwd: str) -> str | None:
    flags = [w for w in words[2:] if w.startswith("-")]
    if not any("f" in f.lstrip("-") or f == "--force" for f in flags):
        return None
    keep = []  # the same flags as a dry run: -fdx -> -dx (plus -n), --force dropped
    for f in flags:
        if f.startswith("--"):
            if f not in ("--force", "--interactive", "--dry-run"):
                keep.append(f)
        elif rest := re.sub(r"[fni]", "", f[1:]):
            keep.append("-" + rest)
    out = _git(cwd, "clean", "-n", *keep, *[w for w in words[2:] if not w.startswith("-")])
    if out is None:
        return None
    files = [l.removeprefix("Would remove ").strip() for l in out.splitlines() if l.strip()]
    if not files:
        return "No untracked files would be deleted"
    return f"Deletes {len(files)} untracked file{'s' if len(files) != 1 else ''} git has never saved: {_names(files)}"


def _checkout(words: list[str], cwd: str) -> str | None:
    sub = words[1]
    if sub == "checkout" and "--" not in words and "." not in words:
        return None
    paths = [w for w in words[words.index("--") + 1:]] if "--" in words else [w for w in words[2:] if not w.startswith("-")]
    if sub == "restore" and ("--staged" in words and "--worktree" not in words):
        return None
    changed = _changed_files(cwd, paths or None)
    if not changed:
        return None
    return f"Overwrites uncommitted changes in {len(changed)} file{'s' if len(changed) != 1 else ''}: {_names(changed)}"


# --------------------------------------------------------------------------- deletes

def delete_targets(cmd: commands.Command, cwd: str) -> list[Path]:
    """The existing paths a delete command names (globs and ~ expanded)."""
    words, prog = cmd.words[1:], cmd.program
    if prog not in DELETE_PROGRAMS:
        return []
    out: list[Path] = []
    skip_next = False
    for w in words:
        if skip_next:
            skip_next = False
            continue
        if w.startswith("-"):
            if prog in ("remove-item", "ri") and w.lower() in ("-path", "-literalpath"):
                continue
            if prog in ("remove-item", "ri") and w.lower() in ("-include", "-exclude", "-filter"):
                skip_next = True
            continue
        if prog in ("rmdir", "rd", "del", "erase") and WINDOWS_FLAG.match(w):
            continue
        w = os.path.expandvars(os.path.expanduser(w.strip("'\"")))
        p = w if os.path.isabs(w) else os.path.join(cwd, w)
        for m in (glob.glob(p) if any(c in p for c in "*?[") else [p]):
            if os.path.lexists(m):
                out.append(Path(m))
    return out


def walk(paths: list[Path], deadline: float) -> tuple[int, int, bool]:
    """(files, bytes, complete) under the paths, stopping at WALK_LIMIT files or the deadline."""
    files = size = 0
    for p in paths:
        if p.is_file() or p.is_symlink():
            files += 1
            size += p.lstat().st_size
            continue
        for root, _dirs, names in os.walk(p):
            for n in names:
                files += 1
                try:
                    size += os.lstat(os.path.join(root, n)).st_size
                except OSError:
                    pass
                if files >= WALK_LIMIT or time.monotonic() > deadline:
                    return files, size, False
    return files, size, True


def _delete(cmd: commands.Command, cwd: str, deadline: float) -> str | None:
    targets = delete_targets(cmd, cwd)
    if not targets:
        return None
    files, size, complete = walk(targets, deadline)
    def shown(t: Path) -> str:  # inside the project: the short path the developer typed
        try:
            return os.path.relpath(t, cwd) if Path(t).resolve().is_relative_to(Path(cwd).resolve()) else str(t)
        except ValueError:      # another drive on Windows
            return str(t)
    where = _names([shown(t) for t in targets], 2)
    more = "+" if not complete else ""
    return f"Deletes {files:,}{more} file{'s' if files != 1 else ''} ({_size(size)}{more}) in {where}"


# --------------------------------------------------------------------------- entry point

def predict(line: str, cwd: str | None = None) -> list[str]:
    """Plain-English lines saying what the command line would change. Empty when there's nothing to measure."""
    cwd = cwd or os.getcwd()
    deadline = time.monotonic() + BUDGET_SECONDS
    out: list[str] = []
    try:
        reading = commands.read(line)
    except Exception:
        return out
    for cmd in reading.commands:
        if time.monotonic() > deadline:
            break
        w = [x for x in cmd.words]
        try:
            if cmd.program == "git" and len(w) > 1:
                fn = {"push": _push, "reset": _reset, "clean": _clean, "checkout": _checkout, "restore": _checkout}.get(w[1])
                msg = fn(w, cwd) if fn else None
            else:
                msg = _delete(cmd, cwd, deadline)
        except Exception:
            msg = None
        if msg and msg not in out:
            out.append(msg[:400])
    return out[:5]
