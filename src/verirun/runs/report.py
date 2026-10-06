"""The static Evidence Pack HTML report: runs/<run-id>/evidence.html.

The Report phase renders the JSON Evidence Pack plus the screenshots in the Run
directory into one standalone document: inline CSS, images inlined as data URIs,
no server and no network. Work Order, Plan, timeline, approvals, verification
results, cost and steps all come straight from the JSON.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"

TEMPLATES = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _timeline_entries(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool Observations and journaled actions as one chronological timeline."""
    entries: list[dict[str, Any]] = []
    for observation in evidence.get("observations") or []:
        detail = observation.get("summary") or ""
        if not observation.get("ok") and observation.get("error_kind"):
            detail = f"{detail} [{observation['error_kind']}]"
        entries.append(
            {
                "at": observation.get("created_at") or "",
                "ok": bool(observation.get("ok")),
                "kind": "tool",
                "title": observation.get("tool") or "(model)",
                "detail": detail,
                "attempt": observation.get("attempt") or 1,
            }
        )
    for action in evidence.get("action_log") or []:
        status = action.get("status")
        detail = action.get("error") or ""
        if not detail and action.get("result"):
            detail = json.dumps(action["result"], ensure_ascii=False)
        entries.append(
            {
                "at": action.get("created_at") or "",
                "ok": status == "done",
                "kind": "action",
                "title": action.get("action") or "(action)",
                "detail": f"{status or 'unknown'}: {detail}" if detail else str(status or "unknown"),
                "attempt": 1,
            }
        )
    entries.sort(key=lambda item: (item["at"], item["kind"]))
    return entries


def _screenshot_candidates(artifact: str, directory: Path | None) -> list[Path]:
    path = Path(artifact)
    candidates = [path]
    if not path.is_absolute() and directory is not None:
        candidates = [directory / path, directory / path.name, path]
    return candidates


def collect_screenshots(
    evidence: dict[str, Any], directory: str | Path | None = None
) -> list[dict[str, str]]:
    """Every PNG screenshot a Run produced, inlined for a standalone report.

    The Run directory is the browser session's artifact directory, so every PNG
    in it belongs to this Run. PNGs referenced by the Run's recorded artifacts
    are also resolved wherever they were written. A missing artifact is skipped
    rather than shown broken.
    """
    root = Path(directory) if directory is not None else None
    files: list[Path] = []
    if root is not None:
        files.extend(sorted(root.glob("*.png")))
    for artifact in evidence.get("artifacts") or []:
        if not str(artifact).lower().endswith(".png"):
            continue
        for candidate in _screenshot_candidates(str(artifact), root):
            if candidate.is_file():
                files.append(candidate)
                break
    unique: dict[str, Path] = {}
    for path in files:
        unique.setdefault(str(path.resolve()), path)
    screenshots: list[dict[str, str]] = []
    for path in unique.values():
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
        screenshots.append(
            {
                "name": path.name,
                "path": str(path),
                "data_uri": f"data:image/png;base64,{encoded}",
            }
        )
    return screenshots


def render_evidence_html(
    evidence: dict[str, Any], *, directory: str | Path | None = None
) -> str:
    """Render the Evidence Pack as one standalone HTML document."""
    return TEMPLATES.get_template("evidence.html").render(
        evidence=evidence,
        timeline=_timeline_entries(evidence),
        screenshots=collect_screenshots(evidence, directory),
    )


def write_evidence_html(evidence_path: str | Path) -> Path:
    """Render evidence.html next to an evidence.json file and return its path."""
    evidence_path = Path(evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    path = evidence_path.with_suffix(".html")
    html = render_evidence_html(payload, directory=evidence_path.parent)
    path.write_text(html, encoding="utf-8")
    return path
