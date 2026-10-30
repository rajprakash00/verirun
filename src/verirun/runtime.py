"""Runtime wiring shared by the CLI and the dashboard."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from verirun.adapters import (
    AccountingReads,
    AccountingSystem,
    MailSource,
    MockAccountingSystem,
    MockMailSource,
)
from verirun.config import Settings
from verirun.context.company import CompanyContext
from verirun.context.task_pack import SCAN_TOOL, TaskPack
from verirun.llm.client import LLMClient
from verirun.tools import (
    BrowserSession,
    PolicyGate,
    ToolRegistry,
    build_browser_tools,
    build_erp_tools,
    build_file_tools,
    build_mail_tools,
)
from verirun.tools.vision import ExtractInvoiceTool


@dataclass(frozen=True)
class Backends:
    """The external systems one Run works against.

    The product path swaps these for an IMAP mail source and a Zoho Books
    accounting system; the Mock Suite wiring stays the default for the demo,
    eval, dashboard, and CLI.
    """

    mail: MailSource
    accounting: AccountingSystem
    reads: AccountingReads | None = None

    @property
    def verifier_accounting(self) -> AccountingReads:
        """What the Verifier reads: the read-only side when one is configured."""
        return self.reads if self.reads is not None else self.accounting

    @classmethod
    def mock_suite(cls, settings: Settings) -> Backends:
        return cls(
            mail=MockMailSource(settings.mail_db, settings.shared_dir),
            accounting=MockAccountingSystem(settings.erp_db),
        )


def build_registry(
    settings: Settings,
    context: CompanyContext,
    task_pack: TaskPack,
    client: LLMClient | None = None,
    *,
    backends: Backends,
    browser: BrowserSession | None = None,
    approval_gates: bool = True,
) -> ToolRegistry:
    """Every tool the Task Pack allows, governed by the policy gate.

    Passing a browser session adds the Playwright tools; the caller owns the
    session's lifecycle and its artifact directory. ``approval_gates`` is an
    eval-only ablation switch: policy denials still apply, but approval rules
    execute instead of parking the Run.
    """
    root = backends.mail.document_root
    tools = [
        *build_file_tools(root),
        *build_mail_tools(backends.mail),
        *build_erp_tools(backends.accounting),
    ]
    if browser is not None:
        tools.extend(build_browser_tools(browser))
    if task_pack.extraction is not None and SCAN_TOOL in task_pack.tools:
        if client is None:
            raise ValueError(
                f"Task Pack '{task_pack.id}' reads scanned documents; "
                "build_registry needs an LLM client"
            )
        tools.append(
            ExtractInvoiceTool(
                root,
                client,
                confidence_threshold=task_pack.extraction.confidence_threshold,
                prices=settings.prices,
            )
        )
    return ToolRegistry.from_task_pack(
        tools,
        task_pack,
        gate=PolicyGate(context.policies, approval_gates=approval_gates),
    )


@contextmanager
def live_registry(
    settings: Settings,
    context: CompanyContext,
    task_pack: TaskPack,
    client: LLMClient,
    run_id: str,
    *,
    backends: Backends,
) -> Iterator[ToolRegistry]:
    """The registry for a live Run, shared by the CLI and the dashboard.

    One browser session is scoped to the Run and writes its artifacts into the
    Run's evidence directory. Chromium starts on the first browser.* call, so a
    Run that never touches the browser neither pays for it nor needs it
    installed. The session carries the LLM client for the vision fallback, and
    closes when the Run returns.
    """
    session = BrowserSession(
        artifact_dir=settings.run_db.parent / run_id,
        client=client,
        prices=settings.prices,
    )
    try:
        yield build_registry(
            settings,
            context,
            task_pack,
            client,
            backends=backends,
            browser=session,
        )
    finally:
        session.close()
