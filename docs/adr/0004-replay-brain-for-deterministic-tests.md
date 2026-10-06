# ADR 0004: Replay brain for deterministic tests

Status: accepted
Date: 2026-10-03
Task: #13 — Evidence HTML and project documentation

## Context

The engine's behaviour depends on a language model, which is nondeterministic,
costs money, and needs network access. The test suite must lock outcomes:
verified results, ground truth in the Mock Suite, the escalation ladder, and the
Evidence Pack. A test strategy that calls a live model cannot do that — the same
run can file the invoice, escalate, or invent a different plan.

At the same time, the product should be runnable in a demo and in development
without an API key.

## Decision

Put the LLM behind a narrow `LLMClient` protocol
(`src/verirun/llm/client.py`) with three implementations selected by
`VERIRUN_LLM_MODE`:

1. **LiveClient** — an OpenAI-compatible chat-completions client. The only
   network call in the system, with a stable per-run session id for prompt
   caching.
2. **ReplayClient** — `record` mode calls the live client and writes one JSON
   fixture per request, keyed by a SHA-256 of model + messages + tools +
   response format; `replay` mode serves those fixtures with no network. A
   missing fixture raises a clear `ReplayMissError` instead of silently passing.
3. **ScriptedClient** (test helper) — a fixed list of assistant turns for the
   acceptance and integration tests. The engine, tools, Mock Suite, run store,
   and Verifier are all real; only the model is scripted.

`scripts/record_llm_fixtures.py` refreshes the committed fixtures for the
Resolve and Plan acceptance paths.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **Live model in tests** | Nondeterministic, slow, costs tokens per run, and fails offline. Cannot assert on exact run states. |
| **Mocking the `LLMClient` everywhere** | Fine for unit tests, but an end-to-end run is a sequence of dozens of context-dependent turns; hand-mocking each call hides the loop under test. The scripted client still exercises the real loop, only fixing its inputs. |
| **Record raw HTTP (e.g. VCR-style cassettes)** | Couples fixtures to transport details and makes them unreadable; keying on the logical request is smaller and clearer. |
| **A fake model with canned heuristics** | Tests would pass against a model that does not exist, and the heuristics become a second implementation to maintain. |

## Consequences

- Tests are deterministic, offline, and free; the failure ladder, approval
  gates, and verification are asserted on exact states and database rows.
- The scripted client tests the engine's response to model output, not the
  model's ability to produce it. Whether the real model plans well is checked
  separately by the recorded Resolve/Plan fixtures and by live demos.
- Fixtures are keyed by an exact request hash, so changing a prompt, the Company
  Context, or the model names invalidates them; they must be re-recorded. The
  recording script warns when the configured models differ from the defaults.
- Replay mode lets a fresh clone run the Resolve/Plan paths with no key; the
  demo and full test suite need no key at all.
