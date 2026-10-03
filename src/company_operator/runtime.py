"""Runtime wiring shared by the CLI and the dashboard."""

from __future__ import annotations

from company_operator.config import Settings
from company_operator.context.company import CompanyContext
from company_operator.context.task_pack import SCAN_TOOL, TaskPack
from company_operator.llm.client import LLMClient
from company_operator.tools import (
    PolicyGate,
    ToolRegistry,
    build_erp_tools,
    build_file_tools,
    build_mail_tools,
)
from company_operator.tools.vision import ExtractInvoiceTool


def build_registry(
    settings: Settings,
    context: CompanyContext,
    task_pack: TaskPack,
    client: LLMClient | None = None,
) -> ToolRegistry:
    """Every tool the Task Pack allows, governed by the policy gate."""
    tools = [
        *build_file_tools(settings.shared_dir),
        *build_mail_tools(settings.mail_db, settings.shared_dir),
        *build_erp_tools(settings.erp_db),
    ]
    if task_pack.extraction is not None and SCAN_TOOL in task_pack.tools:
        if client is None:
            raise ValueError(
                f"Task Pack '{task_pack.id}' reads scanned documents; "
                "build_registry needs an LLM client"
            )
        tools.append(
            ExtractInvoiceTool(
                settings.shared_dir,
                client,
                confidence_threshold=task_pack.extraction.confidence_threshold,
                prices=settings.prices,
            )
        )
    return ToolRegistry.from_task_pack(tools, task_pack, gate=PolicyGate(context.policies))
