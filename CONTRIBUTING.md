# Contributing to Verirun

Thanks for helping. This guide covers the working agreements for this
repository; the product spec is [`SPEC.md`](SPEC.md), the design is
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and the vocabulary is
[`CONTEXT.md`](CONTEXT.md).

## Setup

```bash
uv sync
uv run playwright install chromium          # add --with-deps on a fresh Linux box
cp .env.example .env                        # only needed for live runs
```

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. The test suite
and the demo run offline and need no API key.

## Test

```bash
uv run pytest                               # full suite, offline and deterministic
uv run pytest tests/test_verify.py          # one file while iterating
uv run pytest --cov --cov-report=term-missing
```

Two rules keep the suite trustworthy:

- Tests never call a live model. Use the scripted client in `tests/support.py`
  for new behavior, or `VERIRUN_LLM_MODE=replay` fixtures for recorded
  transcripts.
- Assert external behavior — run state, Mock Suite ground truth, the Evidence
  Pack, dashboard responses — not private functions or transcript text.

Browser tests need Chromium and skip with a clear message when it is missing;
they run in CI, where Chromium is installed.

## Lint

```bash
uv run ruff check .
```

CI runs lint and the test suite on every push and pull request. Coverage is
reported on the run and uploaded to Codecov; the README badge reflects `main`.

## Changing behavior

- **New kind of work**: add a Task Pack under `tasks/`. The engine stays
  task-agnostic; no task-specific branches belong in `src/verirun/engine/`.
- **New tool**: implement the `Tool` interface and register it in the runtime,
  the CLI, and the dashboard through the same construction path.
- **Prompts or Company Context**: replay fixtures are keyed by a hash of the
  exact request. Re-record them with
  `uv run python scripts/record_llm_fixtures.py` after the change.
- **Design decisions**: record them as an ADR in [`docs/adr/`](docs/adr/), and
  update `CONTEXT.md` when vocabulary changes.

## Pull requests

1. Open an issue first for anything larger than a fix; small fixes can go
   straight to a PR. Use the issue templates.
2. Branch from `main`, keep the PR focused, and make sure `uv run pytest` and
   `uv run ruff check .` pass locally.
3. Describe the behavior change and how you verified it. Note any doc updates.
4. A maintainer reviews and merges. Commit history follows the
   `feat:`, `fix:`, `docs:`, `chore:` style already used in the log.

Changes to the repository describe the product and its engineering only. Keep
personal, career, marketing, and launch-planning content out.
