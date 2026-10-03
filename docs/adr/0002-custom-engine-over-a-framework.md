# ADR 0002: Custom engine over an agent framework

Status: accepted
Date: 2026-10-03
Task: #13 — Evidence HTML and project documentation

## Context

Operator must complete real work with guarantees that general-purpose agent
frameworks treat as application code:

- a fixed lifecycle — Resolve, Plan, Approve, Execute, Observe, Verify, Report —
  with every transition persisted;
- Approval Gates that prepare an irreversible action but never submit it until a
  human says yes, and that never time out into an auto-approve;
- a deterministic failure ladder: retry → alternate strategy → re-plan →
  escalate, with retry counts and side effects visible in the journal;
- an action journal with idempotency keys, so a crash and resume never repeat a
  completed side effect;
- Verification as its own phase that reads ground truth and never the executor's
  messages; and
- Task Packs that swap the task without touching the engine.

Frameworks such as LangGraph, CrewAI, AutoGen, and hosted assistants provide
orchestration, but their state, retry, and tool-loop mechanics are the parts we
most need to own and audit. Their multi-agent abstractions add concepts the
product explicitly does not use: there is one orchestrator and one Verifier.

## Decision

Build the engine as a small custom Python state machine
(`src/company_operator/engine/`), on top of an injected LLM client and a uniform
tool interface.

- The lifecycle is an explicit enum with guarded transitions; every phase writes
  to the SQLite run store.
- Execute is a hand-written ReAct loop: one Step at a time, one tool call per
  turn, every observation recorded with its attempt number and failure class.
- The failure ladder and the approval gate live in the executor, where they can
  stop before a side effect rather than after one.
- Task Packs are plain YAML validated at load time; the engine reads them, never
  imports them.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **LangGraph** | Its graph/state model matches the lifecycle, but custom gates, the idempotent journal, and the Verifier phase would still be framework-adjacent code. The graph runtime adds a dependency and indirection without removing the hard parts. |
| **CrewAI / AutoGen** | Multi-agent crews and conversations are the wrong shape: this system is one orchestrator plus a separate Verifier phase, not a society of agents. |
| **Hosted assistants / managed agent APIs** | Hidden state, vendor lock-in, and no way to enforce "prepare but never submit" or to replay deterministically offline. |
| **No abstraction at all** | The opposite failure: without the state enum, run store, and tool protocol, every new behaviour would be ad hoc. The engine is small, but it is still an engine. |

## Consequences

- We own the orchestration code and its tests. The surface is deliberately small
  (a state enum, one executor loop, one policy gate, one verifier), and it stays
  stable when Task Packs change.
- Features frameworks give for free — tracing UIs, streaming, distributed
  workers — are not available and would have to be built if needed.
- The LLM client and Task Packs are decoupled from the engine, so a framework
  could replace the custom loop later without rewriting the domain.
- Determinism is possible end to end: the same scripted turns produce the same
  run, journal, and Evidence Pack.
