# Agent working agreements

## Development

- Setup: `uv sync`
- Browser tests need Chromium: `uv run playwright install chromium` (add `--with-deps` on a fresh Linux box).
- Test: `uv run pytest`
- Lint: `uv run ruff check .`
- CLI: `uv run operator --help`
- Live LLM settings come from `.env` (gitignored); see `.env.example`.
- Read `SPEC.md` (product spec), `docs/ARCHITECTURE.md` (high-level design), and `CONTEXT.md` (glossary) before starting a ticket.

## Agent skills

### Issue tracker

Issues and specs live as GitHub issues in `rajprakash00/proxy-work`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
