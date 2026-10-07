# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-07

First release: the prototype is renamed Verirun, licensed MIT, and packaged
with CI and contributor docs.

### Added

- Run engine: a guarded state machine (`created` → `resolving` → `planned` →
  `executing` → `verifying` → `completed`, plus approval, escalation, failure,
  and limit states) with a fixed failure ladder: retry, alternate strategy,
  re-plan, escalate.
- Approval Gates: irreversible actions are prepared and parked until a human
  approves or rejects them; timeouts never auto-approve.
- Independent Verifier: checks each Task Pack's verification contract against
  LedgerLite state and the filesystem, never the executor's messages.
- Evidence Pack: an `evidence.json` plus a standalone `evidence.html` for every
  Run.
- Two Task Packs on one unchanged engine: invoice processing and vendor
  onboarding.
- Tool layer: files, mail, ERP, and Playwright browser tools driven by
  accessibility-tree refs, plus a vision path for image-only scanned documents.
- Mock Suite: MailDesk webmail and LedgerLite ERP with seeded scenarios, and a
  shared document tree.
- CLI (`verirun run`, `verirun serve`, `verirun report`) and a dashboard with
  the Work Order, Plan, timeline, approval queue, and verification results.
- Demo script that runs three scenarios end to end against the Mock Suite.
- Deterministic offline testing: a scripted LLM client and record/replay
  fixtures; no test calls a live model.
- CI on GitHub Actions: the test suite and lint on every push and pull request,
  with coverage reported and uploaded to Codecov.
- Contributor baseline: MIT license, CONTRIBUTING, SECURITY, issue templates, a
  pull request template, and this changelog.

### Changed

- Renamed the project to Verirun across the package, CLI, environment
  variables, and documentation.

[Unreleased]: https://github.com/rajprakash00/verirun/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rajprakash00/verirun/releases/tag/v0.1.0
