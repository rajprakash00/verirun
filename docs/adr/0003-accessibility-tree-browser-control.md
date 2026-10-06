# ADR 0003: Accessibility-tree browser control over vision-first

Status: accepted
Date: 2026-10-03
Task: #13 — Evidence HTML and project documentation

## Context

The browser is a real tool, not a mock: Verirun opens web apps, reads
pages, fills forms, and captures screenshots. Driving a page requires deciding
how the model perceives it and how it addresses elements. The candidates:

- **Vision-first** (screenshot → model → pixel coordinates): works on any
  rendering, including canvas, but every action costs an image, coordinates are
  brittle across viewport and layout changes, and neither the action nor the
  target is easy to review.
- **CSS selectors**: fast and precise, but selectors are app-specific and encode
  knowledge the model should not be trusted to invent; a changed class name
  breaks the run silently.
- **Accessibility tree**: the page's semantic structure (roles, names, values),
  which is stable, small, textual, and cheap.

The task that matters most — filling ERP and webmail forms — is built from
standard labeled controls, exactly what the accessibility tree represents.

## Decision

Control the browser through the accessibility tree with deterministic refs
(`src/verirun/tools/browser.py`).

- `browser.snapshot` walks the DOM for interactive roles and labeled text,
  assigns each interactive element a short ref (`e1`, `e2`, ...) stored as
  `data-op-ref`, and returns a compact text listing.
- `browser.click`, `browser.type`, `browser.select`, and `browser.extract`
  address elements only by that ref. Refs are re-issued on every snapshot, so
  the model always acts on the page it just saw.
- `browser.screenshot` captures a PNG as an artifact for the Evidence Pack.
  Screenshots are display evidence only; the Verifier never reads them.
- The vision model is reserved for what genuinely has no text layer: image-only
  scanned documents (`files.extract` renders pages and reads fields with
  per-field confidence).

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **Vision-first / computer use** | Every step pays image tokens, coordinates break on scroll and viewport changes, and test runs require a model. It also blurs the audit trail: "clicked at (412, 233)" is not explainable. Kept as a possible fallback for canvas-only UIs, which are out of scope. |
| **CSS selectors authored by the model** | Fragile and unbounded: the model guesses implementation details instead of reading semantics. |
| **Playwright's higher-level APIs by role/name** | Closer, but the snapshot is what makes element identity explicit and cacheable within a page, and it keeps one uniform protocol for all interactive tools. |

## Consequences

- Browser steps are deterministic, cheap, and testable without any LLM: the
  integration tests drive real Chromium through the tool registry.
- The executor can review and cite exactly which labeled control it used, and
  the Evidence Pack can show the screenshot it captured.
- Pages with poor semantics (unlabeled controls, custom widgets, canvas) are
  harder or impossible to drive; this is a documented limitation, not a silent
  failure — a missing ref surfaces as a `not_found` observation the model can
  adapt to.
- Refs are page-scoped. The model must snapshot after every navigation, which
  the tool descriptions and the Execute prompt state explicitly.
