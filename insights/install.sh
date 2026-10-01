#!/usr/bin/env sh
# Squidbrake installer for macOS / Linux.
#   curl -fsSL <server>/install.sh | sh
# With SQUIDBRAKE_PILOT and SQUIDBRAKE_PILOT_SERVER set, it also offers to join that pilot (it asks first).
set -e
printf '\nInstalling Squidbrake (brakes for AI agents)...\n\n'

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is needed: https://www.python.org/downloads/ (macOS: brew install python)"
  exit 1
fi

if command -v pipx >/dev/null 2>&1; then
  PIPX="pipx"
elif command -v brew >/dev/null 2>&1; then
  brew install pipx >/dev/null && PIPX="pipx"
else
  "$PY" -m pip install --user --quiet pipx 2>/dev/null || "$PY" -m pip install --user --quiet --break-system-packages pipx
  PIPX="$PY -m pipx"
fi
(cd /tmp && $PIPX install --force squidbrake && $PIPX ensurepath >/dev/null 2>&1 || true)

SB="$HOME/.local/bin/squidbrake"
[ -x "$SB" ] || SB="squidbrake"
printf '\nInstalled: %s\n' "$("$SB" --version)"

if [ -n "$SQUIDBRAKE_URL" ] && [ -n "$SQUIDBRAKE_AGENT_KEY" ]; then
  # a hosted dashboard: nothing to run locally, just route Claude Code through it
  case "$SQUIDBRAKE_AGENT_KEY" in *YOUR_AGENT_KEY*)
    echo "Put your agent key (from your start page) in place of gw_YOUR_AGENT_KEY and run it again."; exit 1 ;; esac
  if ! curl -fsS -m 20 -H "X-Gateway-Key: $SQUIDBRAKE_AGENT_KEY" "$SQUIDBRAKE_URL/v1/me" >/dev/null; then
    echo "Couldn't reach your dashboard with that key. Check the key and run it again."; exit 1
  fi
  printf '\n  [OK] Your dashboard answers.\n'
  if command -v claude >/dev/null 2>&1 || [ -d "$HOME/.claude" ]; then
    "$SB" connect claude-code --url "$SQUIDBRAKE_URL" --key "$SQUIDBRAKE_AGENT_KEY" --yes --hook-only >/dev/null
    printf '  [OK] Claude Code: every tool call (commands, edits, web, MCP) goes through it.\n'
  fi
  # every other coding agent installed here: its terminal commands and file actions (hooks) ...
  "$SB" connect agents --agent all --url "$SQUIDBRAKE_URL" --key "$SQUIDBRAKE_AGENT_KEY" --yes | sed 's/^/  /'
  # ... and its own MCP servers (GitHub, Stripe, databases...) go through it too
  "$SB" connect guard --agent all --url "$SQUIDBRAKE_URL" --key "$SQUIDBRAKE_AGENT_KEY" --yes | sed 's/^/  /'
  printf '\nLast step: close and reopen your agents (Claude Code, Cursor, ...), then work as usual.\nYour dashboard: %s/dashboard\n\n' "$SQUIDBRAKE_URL"
  exit 0
fi

if [ -n "$SQUIDBRAKE_PILOT" ] && [ -n "$SQUIDBRAKE_PILOT_SERVER" ]; then
  "$SB" pilot join "$SQUIDBRAKE_PILOT" --server "$SQUIDBRAKE_PILOT_SERVER" </dev/tty
fi

cat <<'EOF'

Next:
  1. Open a new terminal (so the 'squidbrake' command is found) and run:  squidbrake
     It prints your keys (save them) and opens the dashboard. Keep that window open.
  2. In another terminal, connect Claude Code:  squidbrake connect claude-code
  3. Restart Claude Code and work as usual. Watch it at http://localhost:8080/dashboard

EOF
