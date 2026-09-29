# Contributing

Thanks for helping! Issues and pull requests are welcome.

## Setup

```bash
./start.sh            # Windows: start.bat  (creates .venv and installs everything)
```

## Tests

```bash
.venv/bin/python -m pip install pytest      # Windows: .venv\Scripts\python
.venv/bin/python -m pytest -q               # unit tests
.venv/bin/python tests/e2e_business_scenario.py   # full scenario: an agent works a sandbox company's inbox
```

Please add a test for any behaviour you change, and keep both suites passing.

## Guidelines

- Keep it simple to run: no new required services or settings. Everything in `.env.example` stays optional.
- Security first: the gateway must stay fail-closed, and a rule that blocks or holds an action must not be bypassable.
- Rules and checks are deterministic. No LLM calls in the decision path.
- Never commit keys, `.env` files or `data/`. `.gitignore` covers them; `MY_*` files are private by convention.

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
