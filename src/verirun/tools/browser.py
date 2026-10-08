"""Browser tools: Playwright with accessibility-tree snapshots and stable refs.

A snapshot walks the accessibility tree of the current page and assigns each
interactive element a short ref (``e1``, ``e2``, ...) stored in a ``data-op-ref``
attribute. ``click``, ``type``, ``select``, and ``extract`` address elements by
that ref, so the executor never has to guess at selectors.

When a ref has gone stale -- the page re-rendered since the snapshot -- the
session re-grounds automatically: it takes a fresh snapshot, re-resolves the
control by its role and accessible name, and retries with the new ref. When the
accessibility tree does not contain the control at all, a screenshot goes to
the vision model, which reports where the control sits on screen, and the
action runs on the element at that point. The resolution an action used is
recorded in its Observation, and therefore in the Run journal, so a retry
replays the same locator instead of repeating a completed side effect.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Self

from playwright.sync_api import (
    Browser,
    BrowserContext,
    ElementHandle,
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
from pydantic import BaseModel, ConfigDict

from verirun.config import ModelPrice
from verirun.engine.models import Observation
from verirun.engine.structured import StructuredOutputError, complete_structured
from verirun.llm.client import AssistantTurn, LLMClient, Message
from verirun.llm.meter import meter_turns
from verirun.tools.base import Tool, ToolError, optional_bool, optional_str, require_str

IDENTITY_JS = r"""
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
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
"""

SNAPSHOT_JS = (
    r"""() => {"""
    + IDENTITY_JS
    + r"""
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const skip = new Set(["script", "style", "noscript", "template", "svg", "path",
                        "head", "meta", "link", "title"]);
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
)

ELEMENT_IDENTITY_JS = (
    r"""el => {"""
    + IDENTITY_JS
    + r"""
  return { role: roleOf(el), name: nameOf(el) };
}"""
)

EXTRACT_JS = (
    "el => (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' "
    "|| el.tagName === 'SELECT') ? el.value : el.innerText"
)

VISION_MIN_CONFIDENCE = 0.5

VISION_SYSTEM_PROMPT = """\
You are the visual fallback of Verirun's browser tools. The accessibility tree
did not contain the control, so you read a screenshot and report where the
control sits. You never guess: if you cannot see the control, you say so.
"""


class ElementLocation(BaseModel):
    """Where the vision model saw the missing control, in screenshot pixels."""

    model_config = ConfigDict(extra="forbid")

    found: bool
    x: int | None = None
    y: int | None = None
    confidence: float = 0.0


def vision_locate_prompt(role: str, name: str, *, width: int, height: int) -> str:
    return (
        f"Find the {role} with the accessible name {name!r} in this screenshot "
        f"(image size {width}x{height} pixels). Reply with one JSON object: "
        '{"found": true, "x": <center x in pixels>, "y": <center y in pixels>, '
        '"confidence": <0 to 1>}. If the control is not visible, reply '
        '{"found": false, "x": null, "y": null, "confidence": 0}.'
    )


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


def _ref_selector(ref: str) -> str:
    return f'[data-op-ref="{ref}"]'


def _element_identity(locator: Locator) -> dict[str, str] | None:
    """The role and accessible name of the element a ref points at now."""
    try:
        payload = locator.evaluate(ELEMENT_IDENTITY_JS)
    except PlaywrightError:
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "role": str(payload.get("role") or ""),
        "name": str(payload.get("name") or ""),
    }


def _identity_matches(actual: dict[str, str], known: dict[str, str]) -> bool:
    return actual["role"] == known["role"] and (
        actual["name"].casefold() == known["name"].casefold()
    )


def _name_matches(actual: str, wanted: str) -> bool:
    actual, wanted = actual.strip().casefold(), wanted.strip().casefold()
    return actual == wanted or (bool(wanted) and wanted in actual)


def _match_node(nodes: list[dict[str, Any]], role: str, name: str) -> dict[str, Any] | None:
    """The interactive node that re-grounds a target by role and name.

    An exact name wins, then a case-insensitive name, then the single node
    whose name contains the wanted one. An ambiguous match is no match.
    """
    candidates = [node for node in nodes if node.get("ref") and node.get("role") == role]
    for node in candidates:
        if node.get("name") == name:
            return node
    for node in candidates:
        if str(node.get("name") or "").casefold() == name.casefold():
            return node
    wanted = name.casefold()
    if not wanted:
        return None
    contains = [
        node for node in candidates if wanted in str(node.get("name") or "").casefold()
    ]
    return contains[0] if len(contains) == 1 else None


def _turns_cost(
    turns: list[AssistantTurn], prices: dict[str, ModelPrice]
) -> tuple[dict[str, Any], float]:
    meter = meter_turns(prices, turns)
    return meter.snapshot(), meter.total_usd


def _element_at(page: Page, point: tuple[int, int]) -> ElementHandle | None:
    try:
        handle = page.evaluate_handle(
            "([x, y]) => document.elementFromPoint(x, y)", list(point)
        )
    except PlaywrightError as exc:
        raise ToolError(
            "not_found", f"cannot inspect the page at {point}: {_first_line(exc)}"
        ) from exc
    return handle.as_element()


def _try_element_at(page: Page, point: tuple[int, int]) -> ElementHandle | None:
    try:
        return _element_at(page, point)
    except ToolError:
        return None


def _vision_mismatch(element: ElementHandle, role: str, name: str) -> str | None:
    """How the element at a vision point contradicts the wanted control, if it does."""
    try:
        payload = element.evaluate(ELEMENT_IDENTITY_JS)
    except PlaywrightError:
        return None
    actual = payload if isinstance(payload, dict) else {}
    actual_role = str(actual.get("role") or "")
    actual_name = str(actual.get("name") or "")
    if actual_role and actual_role != role:
        return f"the element there is a {actual_role}, not a {role}"
    if actual_name and not _name_matches(actual_name, name):
        return f"the element there is named {actual_name!r}, not {name!r}"
    return None


class ResolveMethod(StrEnum):
    """How a browser action resolved its target."""

    REF = "ref"
    REPLAY = "replay"
    SNAPSHOT = "snapshot"
    VISION = "vision"


@dataclass(frozen=True)
class ResolvedTarget:
    """One resolved browser control, ready for an action.

    ``method`` says how it was resolved: the requested ref was still current,
    a locator resolved earlier was replayed, a fresh snapshot matched the role
    and accessible name, or the vision model located it on screen.
    """

    requested_ref: str
    role: str
    name: str
    method: ResolveMethod
    ref: str | None = None
    locator: Locator | None = None
    element: ElementHandle | None = None
    point: tuple[int, int] | None = None
    confidence: float | None = None
    artifacts: tuple[str, ...] = ()
    usage: dict[str, Any] | None = None
    cost_usd: float = 0.0

    @property
    def regrounded(self) -> bool:
        return self.method is not ResolveMethod.REF

    @property
    def handle(self) -> Locator | ElementHandle:
        handle = self.locator if self.locator is not None else self.element
        if handle is None:
            raise ToolError("unknown", "the resolved target has no locator")
        return handle

    def click(self) -> None:
        self.handle.click()

    def fill(self, text: str) -> None:
        self.handle.fill(text)

    def press(self, key: str) -> None:
        self.handle.press(key)

    def select_option(self, value: str) -> None:
        self.handle.select_option(value=value)

    def evaluate(self, expression: str) -> Any:
        return self.handle.evaluate(expression)

    def journal_data(self, **extra: Any) -> dict[str, Any]:
        """The Observation payload: the requested ref plus the locator used."""
        locator: dict[str, Any] = {
            "role": self.role,
            "name": self.name,
            "method": self.method.value,
        }
        if self.ref is not None:
            locator["ref"] = self.ref
        if self.point is not None:
            locator["point"] = list(self.point)
        if self.confidence is not None:
            locator["confidence"] = self.confidence
        data: dict[str, Any] = {
            "ref": self.requested_ref,
            "locator": locator,
            "regrounded": self.regrounded,
            **extra,
        }
        if self.usage is not None:
            data["usage"] = self.usage
        if self.cost_usd > 0:
            data["cost_usd"] = self.cost_usd
        return data


def _summary(text: str, target: ResolvedTarget) -> str:
    if target.regrounded:
        return f"{text} (re-grounded via {target.method.value})"
    return text


class BrowserSession:
    """One Chromium session shared by the browser tools of a Run."""

    def __init__(
        self,
        artifact_dir: str | Path,
        *,
        headless: bool = True,
        timeout_ms: int = 5_000,
        client: LLMClient | None = None,
        prices: dict[str, ModelPrice] | None = None,
    ) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._client = client
        self._prices = dict(prices or {})
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._screenshots = 0
        self._known_refs: dict[str, dict[str, str]] = {}
        self._resolved_refs: dict[str, str] = {}
        self._vision_locators: dict[str, tuple[str, int, int, float]] = {}

    def start(self) -> BrowserSession:
        if self._page is not None:
            return self
        try:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            self._screenshots = len(list(self.artifact_dir.glob("screenshot-*.png")))
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self._context = self._browser.new_context(viewport={"width": 1280, "height": 900})
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        """Tear down whatever started, never raising from cleanup."""
        for close in (
            self._context.close if self._context else None,
            self._browser.close if self._browser else None,
            self._playwright.stop if self._playwright else None,
        ):
            if close is not None:
                with suppress(Exception):
                    close()
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
        """The current page, starting Chromium on first use."""
        if self._page is None:
            try:
                self.start()
            except PlaywrightError as exc:
                raise ToolError(
                    "unknown",
                    f"the browser failed to start: {_first_line(exc)}; "
                    "install Chromium with `uv run playwright install chromium`",
                ) from exc
        assert self._page is not None
        return self._page

    def snapshot(self) -> dict[str, Any]:
        """A fresh accessibility snapshot; its refs become the model's view."""
        payload = self._fresh_snapshot()
        for node in payload.get("nodes") or []:
            if node.get("ref"):
                self._known_refs[str(node["ref"])] = {
                    "role": str(node.get("role") or ""),
                    "name": str(node.get("name") or ""),
                }
        return payload

    def _fresh_snapshot(self) -> dict[str, Any]:
        """A raw snapshot. Internal re-grounding never redefines the model's refs."""
        return _web_action(
            lambda: self.page.evaluate(SNAPSHOT_JS),
            what="taking a snapshot",
        )

    def resolve(self, ref: str) -> ResolvedTarget:
        """The control a ref names, re-grounding when the ref has gone stale."""
        known = self._known_refs.get(ref)
        current = self.page.locator(_ref_selector(ref))
        if current.count() > 0:
            actual = _element_identity(current.first)
            if actual is not None and (known is None or _identity_matches(actual, known)):
                return ResolvedTarget(
                    requested_ref=ref,
                    role=actual["role"],
                    name=actual["name"],
                    method=ResolveMethod.REF,
                    ref=ref,
                    locator=current.first,
                )
        if known is None:
            raise ToolError(
                "not_found",
                f"no element with ref '{ref}' on this page; take a new snapshot",
            )
        replay = self._resolved_refs.get(ref)
        if replay is not None:
            replayed = self.page.locator(_ref_selector(replay))
            if replayed.count() > 0:
                actual = _element_identity(replayed.first)
                if actual is not None and _identity_matches(actual, known):
                    return ResolvedTarget(
                        requested_ref=ref,
                        role=known["role"],
                        name=known["name"],
                        method=ResolveMethod.REPLAY,
                        ref=replay,
                        locator=replayed.first,
                    )
        return self._reground(ref, known["role"], known["name"])

    def _reground(self, requested_ref: str, role: str, name: str) -> ResolvedTarget:
        """Fresh snapshot, then re-resolve by role and accessible name."""
        payload = self._fresh_snapshot()
        match = _match_node(list(payload.get("nodes") or []), role, name)
        if match is not None:
            new_ref = str(match["ref"])
            self._resolved_refs[requested_ref] = new_ref
            return ResolvedTarget(
                requested_ref=requested_ref,
                role=role,
                name=name,
                method=ResolveMethod.SNAPSHOT,
                ref=new_ref,
                locator=self.page.locator(_ref_selector(new_ref)).first,
            )
        return self._vision_reground(requested_ref, role, name)

    def _vision_reground(self, requested_ref: str, role: str, name: str) -> ResolvedTarget:
        """Screenshot plus the vision model, for a control the tree lacks.

        A locator the vision fallback resolved before is replayed while the page
        URL is unchanged, so a retry touches the same point without another
        model call.
        """
        cached = self._vision_locators.get(requested_ref)
        if cached is not None and cached[0] == self.page.url:
            point = (cached[1], cached[2])
            element = _try_element_at(self.page, point)
            if element is not None and _vision_mismatch(element, role, name) is None:
                return ResolvedTarget(
                    requested_ref=requested_ref,
                    role=role,
                    name=name,
                    method=ResolveMethod.REPLAY,
                    element=element,
                    point=point,
                    confidence=cached[3],
                )
            self._vision_locators.pop(requested_ref, None)
        if self._client is None:
            raise ToolError(
                "not_found",
                f"no {role} named {name!r} is in the accessibility tree, and no "
                "vision model is configured for the visual fallback",
                data={"locator": {"role": role, "name": name, "method": "vision"}},
            )
        path = self.screenshot_path(f"reground-{role}-{name}")
        _web_action(
            lambda: self.page.screenshot(path=str(path), full_page=False),
            what="taking a screenshot for the vision fallback",
        )
        viewport = self.page.viewport_size or {"width": 1280, "height": 900}
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        messages: list[Message] = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": vision_locate_prompt(
                            role, name, width=viewport["width"], height=viewport["height"]
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            },
        ]
        turns: list[AssistantTurn] = []
        try:
            location = complete_structured(
                self._client,
                messages,
                ElementLocation,
                model_role="vision",
                on_turn=turns.append,
            )
        except StructuredOutputError as exc:
            raise ToolError(
                "not_found",
                f"the vision fallback could not locate the {role} named {name!r}: {exc}",
                data={"locator": {"role": role, "name": name, "method": "vision"}},
            ) from exc
        usage, cost = _turns_cost(turns, self._prices)
        if (
            not location.found
            or location.x is None
            or location.y is None
            or location.confidence < VISION_MIN_CONFIDENCE
        ):
            raise ToolError(
                "not_found",
                f"the vision fallback did not find the {role} named {name!r} on screen",
                data={
                    "locator": {"role": role, "name": name, "method": "vision"},
                    "usage": usage,
                    "cost_usd": cost,
                },
            )
        point = (int(location.x), int(location.y))
        element = _element_at(self.page, point)
        mismatch = None if element is None else _vision_mismatch(element, role, name)
        if element is None or mismatch is not None:
            problem = "where there is no element" if element is None else f"but {mismatch}"
            raise ToolError(
                "not_found",
                f"the vision fallback pointed at ({point[0]}, {point[1]}), {problem}",
                data={
                    "locator": {
                        "role": role,
                        "name": name,
                        "method": "vision",
                        "point": list(point),
                        "confidence": location.confidence,
                    },
                    "usage": usage,
                    "cost_usd": cost,
                },
            )
        self._vision_locators[requested_ref] = (
            self.page.url,
            point[0],
            point[1],
            location.confidence,
        )
        return ResolvedTarget(
            requested_ref=requested_ref,
            role=role,
            name=name,
            method=ResolveMethod.VISION,
            element=element,
            point=point,
            confidence=location.confidence,
            artifacts=(str(path),),
            usage=usage,
            cost_usd=cost,
        )

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
        payload = self.session.snapshot()
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
    description = (
        "Click the element with the given ref from the latest snapshot. A stale "
        "ref is re-grounded by role and name, and the vision fallback locates a "
        "control the accessibility tree lacks."
    )
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
        target = self.session.resolve(ref)
        _web_action(lambda: target.click(), what=f"clicking {ref}")
        _web_action(
            lambda: self.session.page.wait_for_load_state("load"),
            what="waiting for the page after the click",
        )
        return Observation(
            ok=True,
            summary=_summary(f"Clicked {ref}", target),
            data=target.journal_data(url=self.session.page.url),
            artifacts=list(target.artifacts),
        )


class TypeTool(BrowserTool):
    name = "browser.type"
    description = (
        "Fill a text field with the given ref, re-grounding a stale ref and "
        "falling back to vision for a control the accessibility tree lacks. "
        "Optionally press Enter to submit."
    )
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
        target = self.session.resolve(ref)
        _web_action(lambda: target.fill(text), what=f"typing into {ref}")
        if submit:
            _web_action(lambda: target.press("Enter"), what=f"submitting {ref}")
            _web_action(
                lambda: self.session.page.wait_for_load_state("load"),
                what="waiting for the page after submit",
            )
        return Observation(
            ok=True,
            summary=_summary(f"Typed {text!r} into {ref}", target),
            data=target.journal_data(text=text),
            artifacts=list(target.artifacts),
        )


class SelectTool(BrowserTool):
    name = "browser.select"
    description = (
        "Choose an option in a select field by its value, then by its label, "
        "re-grounding a stale ref when the page changed."
    )
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
        target = self.session.resolve(ref)
        options = _web_action(
            lambda: target.evaluate(
                "el => Array.from(el.options || []).map((option) => "
                "({value: option.value, label: option.text.trim()}))"
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
        _web_action(lambda: target.select_option(match), what=f"selecting {value!r}")
        return Observation(
            ok=True,
            summary=_summary(f"Selected {match!r} in {ref}", target),
            data=target.journal_data(value=match),
            artifacts=list(target.artifacts),
        )


class ExtractTool(BrowserTool):
    name = "browser.extract"
    description = (
        "Extract text: a field's value or an element's text by ref, or the page "
        "text. A stale ref is re-grounded by role and name."
    )
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
            target = self.session.resolve(ref)
            text = _web_action(lambda: target.evaluate(EXTRACT_JS), what=f"extracting {ref}")
            return Observation(
                ok=True,
                summary=_summary(f"Extracted {len(text)} characters from {ref}", target),
                data=target.journal_data(text=text, url=page.url),
                artifacts=list(target.artifacts),
            )
        text = _web_action(
            lambda: page.inner_text("body"),
            what="extracting the page text",
        )
        return Observation(
            ok=True,
            summary=f"Extracted {len(text)} characters",
            data={"ref": None, "text": text, "url": page.url},
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
