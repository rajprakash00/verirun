"""Runtime wiring shared by the CLI and the dashboard."""

from __future__ import annotations

from company_operator.config import Settings
from company_operator.context.company import CompanyContext
from company_operator.context.task_pack import TaskPack
from company_operator.tools import (
    PolicyGate,
    ToolRegistry,
    build_erp_tools,
    build_file_tools,
    build_mail_tools,
)


def build_registry(
    settings: Settings, context: CompanyContext, task_pack: TaskPack
) -> ToolRegistry:
    """Every tool the Task Pack allows, governed by the policy gate."""
    tools = [
        *build_file_tools(settings.shared_dir),
        *build_mail_tools(settings.mail_db, settings.shared_dir),
        *build_erp_tools(settings.erp_db),
    ]
    return ToolRegistry.from_task_pack(tools, task_pack, gate=PolicyGate(context.policies))
