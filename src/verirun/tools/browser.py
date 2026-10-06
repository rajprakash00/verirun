"""Browser tools: Playwright with accessibility-tree snapshots and stable refs.

A snapshot walks the accessibility tree of the current page and assigns each
interactive element a short ref (``e1``, ``e2``, ...) stored in a ``data-op-ref``
attribute. ``click``, ``type``, ``select``, and ``extract`` address elements by
that ref, so the executor never has to guess at selectors.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Self

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from verirun.engine.models import Observation
from verirun.tools.base import Tool, ToolError, optional_bool, optional_str, require_str

SNAPSHOT_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const skip = new Set(["script", "style", "noscript", "template", "svg", "path",
                        "head", "meta", "link", "title"]);
  const roleOf = (el) => {
    const explicit = (el.getAttribute("role") || "").trim();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "hidden") return null;
      return "textbox";
    }
    return null;
  };
  const nameOf = (el) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return clean(aria);
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/).map((id) => {
        const node = document.getElementById(id);
        return node ? clean(node.textContent) : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    const id = el.getAttribute("id");
    if (id) {
      const forLabel = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (forLabel) return clean(forLabel.textContent);
    }
    const wrapping = el.closest("label");
    if (wrapping) {
      const clone = wrapping.cloneNode(true);
      clone.querySelectorAll("input, select, textarea, button").forEach((node) => node.remove());
      const text = clean(clone.textContent);
      if (text) return text;
    }
    if (el.tagName === "INPUT") {
      return clean(el.getAttribute("placeholder") || el.getAttribute("name") || "");
    }
    return clean(el.getAttribute("title") || el.getAttribute("name") || el.textContent || "");
  };
  const nodes = [];
  let counter = 0;
  document.querySelectorAll("[data-op-ref]").forEach((el) => el.removeAttribute("data-op-ref"));
  document.querySelectorAll("*").forEach((el) => {
    const tag = el.tagName.toLowerCase();
    if (skip.has(tag) || !visible(el)) return;
    const role = roleOf(el);
    if (role) {
      const node = { role: role, name: nameOf(el), ref: null };
      if (["link", "button", "textbox", "combobox", "checkbox", "radio"].includes(role)) {
        counter += 1;
        node.ref = "e" + counter;
        el.setAttribute("data-op-ref", node.ref);
      }
      if (tag === "input" || tag === "textarea" || tag === "select") {
        node.value = el.type === "password" ? "***" : el.value;
      }
      if (tag === "select") {
        node.options = Array.from(el.options).map((option) => ({
          value: option.value,
          label: clean(option.text),
        }));
      }
      nodes.push(node);
      return;
    }
    if (/^h[1-6]$/.test(tag)) {
      const text = clean(el.textContent);
      if (text) nodes.push({ role: "heading", name: text, ref: null, level: Number(tag[1]) });
      return;
    }
    if (["p", "li", "td", "th", "dt", "dd", "pre", "blockquote"].includes(tag)) {
      const hasElementChildren = Array.from(el.children).some(
        (child) => !skip.has(child.tagName.toLowerCase())
      );
      if (hasElementChildren) return;
      const text = clean(el.textContent);
      if (text) nodes.push({ role: "text", name: text, ref: null });
    }
  });
  return { url: location.href, title: document.title, nodes: nodes };
}
"""


def _format_snapshot(nodes: list[dict[str, Any]]) -> str:
    lines = []
    for node in nodes:
        line = f'- {node["role"]} "{node["name"]}"'
        if node.get("ref"):
            line += f' [ref={node["ref"]}]'
        if node.get("value"):
            line += f' value="{node["value"]}"'
        if node.get("options"):
            line += f' options={len(node["options"])}'
        lines.append(line)
    return "\n".join(lines)


class BrowserSession:
    """One Chromium session shared by the browser tools of a Run."""

    def __init__(
        self,
        artifact_dir: str | Path,
        *,
        headless: bool = True,
        timeout_ms: int = 5_000,
    ) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._screenshots = 0

    def start(self) -> BrowserSession:
        if self._page is not None:
            return self
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._screenshots = len(list(self.artifact_dir.glob("screenshot-*.png")))
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        return self

    def close(self) -> None:
        for close in (
            self._context.close if self._context else None,
            self._browser.close if self._browser else None,
            self._playwright.stop if self._playwright else None,
        ):
            if close is not None:
                try:
                    close()
                except PlaywrightError:
                    pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise ToolError("unknown", "the browser session has not been started")
        return self._page

    def locator(self, ref: str) -> Locator:
        locator = self.page.locator(f'[data-op-ref="{ref}"]')
        if locator.count() == 0:
            raise ToolError(
                "not_found",
                f"no element with ref '{ref}' on this page; take a new snapshot",
            )
        return locator.first

    def screenshot_path(self, name: str | None = None) -> Path:
        if name:
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "screenshot"
            if not stem.lower().endswith(".png"):
                stem += ".png"
            return self.artifact_dir / stem
        self._screenshots += 1
        return self.artifact_dir / f"screenshot-{self._screenshots:03d}.png"


def _web_action(perform: Callable[[], Any], what: str) -> Any:
    try:
        return perform()
    except PlaywrightTimeoutError as exc:
        raise ToolError("transient", f"{what} timed out: {_first_line(exc)}") from exc
    except PlaywrightError as exc:
        raise ToolError("unknown", f"{what} failed: {_first_line(exc)}") from exc


def _first_line(error: Exception) -> str:
    return str(error).splitlines()[0] if str(error) else type(error).__name__


class BrowserTool(Tool):
    def __init__(self, session: BrowserSession) -> None:
        self.session = session


class NavigateTool(BrowserTool):
    name = "browser.navigate"
    description = "Open an absolute http(s) URL in the browser and wait for the page to load."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL to open."}
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        url = require_str(args, "url")
        if not url.startswith(("http://", "https://")):
            raise ToolError("invalid", f"url must be absolute http(s), got {url!r}")
        page = self.session.page
        response = _web_action(
            lambda: page.goto(url, wait_until="load", timeout=self.session.timeout_ms),
            what=f"navigating to {url}",
        )
        title = page.title()
        return Observation(
            ok=True,
            summary=f"Navigated to {page.url} ({title})",
            data={
                "url": page.url,
                "title": title,
                "status": response.status if response is not None else None,
            },
        )


class SnapshotTool(BrowserTool):
    name = "browser.snapshot"
    description = (
        "Take an accessibility snapshot of the current page. Interactive elements "
        "carry stable refs to pass to click, type, select, and extract."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        payload = _web_action(
            lambda: self.session.page.evaluate(SNAPSHOT_JS),
            what="taking a snapshot",
        )
        nodes = list(payload.get("nodes") or [])
        refs = {node["ref"]: node for node in nodes if node.get("ref")}
        snapshot = _format_snapshot(nodes)
        return Observation(
            ok=True,
            summary=(
                f"Snapshot of {payload.get('title', '')} at {payload.get('url', '')} "
                f"with {len(refs)} refs"
            ),
            data={
                "url": payload.get("url"),
                "title": payload.get("title"),
                "snapshot": snapshot,
                "nodes": nodes,
                "refs": refs,
            },
        )


class ClickTool(BrowserTool):
    name = "browser.click"
    description = "Click the element with the given ref from the latest snapshot."
    side_effect = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "Element ref from browser.snapshot."}
        },
        "required": ["ref"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        ref = require_str(args, "ref")
        locator = self.session.locator(ref)
        _web_action(lambda: locator.click(), what=f"clicking {ref}")
        _web_action(
            lambda: self.session.page.wait_for_load_state("load"),
            what="waiting for the page after the click",
        )
        return Observation(
            ok=True,
            summary=f"Clicked {ref}",
            data={"ref": ref, "url": self.session.page.url},
        )


class TypeTool(BrowserTool):
    name = "browser.type"
    description = "Fill a text field with the given ref. Optionally press Enter to submit."
    side_effect = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "Element ref from browser.snapshot."},
            "text": {"type": "string", "description": "Text to fill into the field."},
            "submit": {
                "type": "boolean",
                "description": "Press Enter after filling. Defaults to false.",
            },
        },
        "required": ["ref", "text"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        ref = require_str(args, "ref")
        text = args.get("text")
        if not isinstance(text, str):
            raise ToolError("invalid", "missing required argument 'text'")
        submit = optional_bool(args, "submit", False)
        locator = self.session.locator(ref)
        _web_action(lambda: locator.fill(text), what=f"typing into {ref}")
        if submit:
            _web_action(lambda: locator.press("Enter"), what=f"submitting {ref}")
            _web_action(
                lambda: self.session.page.wait_for_load_state("load"),
                what="waiting for the page after submit",
            )
        return Observation(
            ok=True,
            summary=f"Typed {text!r} into {ref}",
            data={"ref": ref, "text": text},
        )


class SelectTool(BrowserTool):
    name = "browser.select"
    description = "Choose an option in a select field by its value, then by its label."
    side_effect = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "Element ref from browser.snapshot."},
            "value": {"type": "string", "description": "Option value or label to select."},
        },
        "required": ["ref", "value"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        ref = require_str(args, "ref")
        value = require_str(args, "value")
        locator = self.session.locator(ref)
        options = _web_action(
            lambda: locator.locator("option").evaluate_all(
                "els => els.map((el) => ({value: el.value, label: el.text.trim()}))"
            ),
            what=f"reading options from {ref}",
        )
        match = next((option["value"] for option in options if option["value"] == value), None)
        if match is None:
            match = next(
                (option["value"] for option in options if option["label"] == value), None
            )
        if match is None:
            match = next(
                (
                    option["value"]
                    for option in options
                    if value.lower() in option["label"].lower()
                ),
                None,
            )
        if match is None:
            available = ", ".join(option["value"] for option in options) or "(none)"
            raise ToolError(
                "invalid",
                f"{ref} has no option {value!r}; available values: {available}",
            )
        _web_action(lambda: locator.select_option(value=match), what=f"selecting {value!r}")
        return Observation(
            ok=True,
            summary=f"Selected {match!r} in {ref}",
            data={"ref": ref, "value": match},
        )


class ExtractTool(BrowserTool):
    name = "browser.extract"
    description = "Extract text: a field's value or an element's text by ref, or the page text."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Optional element ref from browser.snapshot. Omit for page text.",
            }
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        ref = optional_str(args, "ref")
        page = self.session.page
        if ref:
            locator = self.session.locator(ref)
            text = _web_action(
                lambda: locator.evaluate(
                    "el => (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' "
                    "|| el.tagName === 'SELECT') ? el.value : el.innerText"
                ),
                what=f"extracting {ref}",
            )
        else:
            text = _web_action(
                lambda: page.inner_text("body"),
                what="extracting the page text",
            )
        return Observation(
            ok=True,
            summary=f"Extracted {len(text)} characters" + (f" from {ref}" if ref else ""),
            data={"ref": ref, "text": text, "url": page.url},
        )


class ScreenshotTool(BrowserTool):
    name = "browser.screenshot"
    description = "Save a full-page PNG screenshot and return its path as an artifact."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Optional file name for the screenshot. Defaults to a counter.",
            }
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        path = self.session.screenshot_path(optional_str(args, "name"))
        _web_action(
            lambda: self.session.page.screenshot(path=str(path), full_page=True),
            what="taking a screenshot",
        )
        return Observation(
            ok=True,
            summary=f"Screenshot saved to {path}",
            data={"path": str(path)},
            artifacts=[str(path)],
        )


def build_browser_tools(session: BrowserSession) -> list[Tool]:
    """The browser tools, sharing one session and one artifact directory."""
    return [
        NavigateTool(session),
        SnapshotTool(session),
        ClickTool(session),
        TypeTool(session),
        SelectTool(session),
        ExtractTool(session),
        ScreenshotTool(session),
    ]
