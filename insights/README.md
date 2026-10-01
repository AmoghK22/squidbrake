# Squidbrake Insights

A small service for running pilots: you send a founder a link, they install Squidbrake from it, and you see whether
and how they use it, **as counts only**.

- **`/start/<code>`**: the page you send. Their install command, with their pilot code filled in. No GitHub needed.
- **`/install.ps1`, `/install.sh`**: one-line installers (pipx + `squidbrake`), which also offer to join the pilot.
- **`/admin`**: your dashboard. Create a pilot (you get a link and a message to send), then see each pilot's stage
  (link sent → opened → installed → active / gone quiet), actions per day, what was held, approved, rejected or
  blocked, which agents are connected and which rules fired.

## What a pilot install sends

Only after `squidbrake pilot join CODE --server URL`, which shows the list below and asks first
(see [`pilot.py`](../pilot.py)):

- Squidbrake version, OS, enforce/shadow mode, number of rules
- agent labels (e.g. `claude-code`) and how many actions each made
- per day, for the last 7 days: actions allowed, held, approved, rejected, blocked, timed out, failed
- the ids of the rules that blocked or held things

**Never:** commands, code, files, prompts, tool inputs or outputs, keys, people's names. `squidbrake pilot leave` stops it.

## Run it

```bash
docker build -t squidbrake-insights insights/
docker run -d -p 8090:8090 -v insights-data:/data \
  -e INSIGHTS_ADMIN_KEY=$(openssl rand -hex 24) \
  -e INSIGHTS_PUBLIC_URL=https://insights.example.com \
  -e INSIGHTS_CONTACT="you@example.com" \
  squidbrake-insights
```

Put it behind HTTPS (pilot installs only send to `https://` servers).
