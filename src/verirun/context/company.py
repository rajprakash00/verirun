from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from verirun.context.models import (
    CompanyContext,
    CompanyContextError,
    Precedent,
    Sop,
    SystemEntry,
)
from verirun.context.policies import Policy, PolicySet
from verirun.validation import format_validation_errors


def load_company_context(root: str | Path) -> CompanyContext:
    root = Path(root)
    if not root.is_dir():
        raise CompanyContextError(f"company context not found: {root}")
    sops = {
        doc_id: Sop(id=doc_id, title=title, body=body)
        for doc_id, title, body in _load_markdown_dir(root / "sops")
    }
    precedents = [
        Precedent(id=doc_id, title=title, body=body)
        for doc_id, title, body in _load_markdown_dir(root / "precedents")
    ]
    return CompanyContext(
        root=root,
        sops=sops,
        policies=PolicySet(_load_policies(root / "policies")),
        systems=_load_systems(root / "systems.yaml"),
        precedents=precedents,
    )


def _load_markdown_dir(directory: Path) -> list[tuple[str, str, str]]:
    if not directory.is_dir():
        return []
    documents: list[tuple[str, str, str]] = []
    for path in sorted(directory.glob("*.md")):
        doc_id = path.stem
        text = path.read_text(encoding="utf-8")
        title, body = _split_title(doc_id, text)
        documents.append((doc_id, title, body))
    return documents


def _split_title(doc_id: str, text: str) -> tuple[str, str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            body = "\n".join(lines[index + 1 :]).strip()
            return title, body
    return doc_id.replace("-", " ").title(), text.strip()


def _load_policies(directory: Path) -> list[Policy]:
    if not directory.is_dir():
        return []
    policies: list[Policy] = []
    seen: dict[str, Path] = {}
    for path in sorted(directory.glob("*.y*ml")):
        data = _read_yaml(path)
        try:
            policy = Policy.model_validate(data)
        except ValidationError as exc:
            raise CompanyContextError(
                f"invalid policy {path.name}: {format_validation_errors(exc)}"
            ) from exc
        if policy.id in seen:
            raise CompanyContextError(
                f"duplicate policy id '{policy.id}' in {path.name} "
                f"(already defined in {seen[policy.id].name})"
            )
        seen[policy.id] = path
        policies.append(policy)
    return policies


def _load_systems(path: Path) -> dict[str, SystemEntry]:
    if not path.is_file():
        raise CompanyContextError(f"system registry not found: {path}")
    data = _read_yaml(path)
    entries = data.get("systems") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise CompanyContextError(f"invalid system registry {path.name}: expected a 'systems' list")
    systems: dict[str, SystemEntry] = {}
    for entry in entries:
        try:
            system = SystemEntry.model_validate(entry)
        except ValidationError as exc:
            raise CompanyContextError(
                f"invalid system registry {path.name}: {format_validation_errors(exc)}"
            ) from exc
        if system.id in systems:
            raise CompanyContextError(
                f"duplicate system id '{system.id}' in {path.name}"
            )
        systems[system.id] = system
    return systems


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CompanyContextError(f"invalid YAML in {path.name}: {exc}") from exc
