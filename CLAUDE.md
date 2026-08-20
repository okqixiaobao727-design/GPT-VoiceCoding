## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues, operated via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles; `needs-triage` maps to the existing `need triage` label, the rest use their default names. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Tests and lint

Run both through the project venv, which carries the editable install of the package:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

A bare `pytest` or `ruff` reaches the system Python, where the package is absent and `ruff` is
not installed. Build a missing venv the way CI does: `python3 -m venv .venv && .venv/bin/pip
install -e '.[dev]'`.
