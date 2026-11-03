"""Runtime wiring shared by the CLI and the dashboard."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verirun.adapters import (
    AccountingReads,
    AccountingSystem,
    ImapSource,
    MailSource,
    MockAccountingSystem,
    MockMailSource,
    WatchedFolderSource,
)
from verirun.adapters.zoho import (
    ZohoAccountingReads,
    ZohoAccountingSystem,
    ZohoBooksClient,
    ZohoOAuthConfig,
    ZohoTokenManager,
    ZohoTokenStore,
)
from verirun.config import Settings
from verirun.context.company import CompanyContext
from verirun.context.task_pack import INVOICE_EXTRACT_TOOL, SCAN_TOOL, TaskPack
from verirun.llm.client import LLMClient
from verirun.runs.store import RunStore
from verirun.tools import (
    BrowserSession,
    PolicyGate,
    ToolRegistry,
    build_browser_tools,
    build_erp_tools,
    build_file_tools,
    build_mail_tools,
)
from verirun.tools.invoice import ExtractIndiaInvoiceTool
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

    @classmethod
    def from_settings(cls, settings: Settings) -> Backends:
        """The product mail source and Zoho Books reads when configured.

        Zoho Books is used only when its configuration is complete. A partial
        configuration is an error: silently falling back to the Mock Suite
        would file a real invoice in the wrong system.
        """
        if settings.imap_host:
            mail: MailSource = ImapSource(
                host=settings.imap_host,
                port=settings.imap_port,
                username=settings.imap_username,
                password=settings.imap_password.get_secret_value(),
                document_root=settings.shared_dir,
                state_db=settings.mail_state_db,
                folder=settings.imap_folder,
                use_ssl=settings.imap_ssl,
            )
        elif settings.watch_dir is not None:
            mail = WatchedFolderSource(
                settings.watch_dir,
                settings.shared_dir,
                settings.mail_state_db,
            )
        else:
            mail = MockMailSource(settings.mail_db, settings.shared_dir)
        zoho = _zoho_accounting(settings)
        if zoho is None:
            return cls(mail=mail, accounting=MockAccountingSystem(settings.erp_db))
        reads, accounting = zoho
        return cls(mail=mail, accounting=accounting, reads=reads)


def _zoho_accounting(settings: Settings) -> tuple[ZohoAccountingReads, ZohoAccountingSystem] | None:
    """Build the read-only Verifier client and the write-capable executor client.

    The two clients hold separate OAuth refresh tokens. If either half is
    partially configured the whole integration is an error: silently falling
    back to LedgerLite would file a real invoice in the wrong system, and
    falling back to the read token would fail at the first bill.
    """
    configured = {
        "VERIRUN_ZOHO_ORGANIZATION_ID": settings.zoho_organization_id,
        "VERIRUN_ZOHO_CLIENT_ID": settings.zoho_client_id,
        "VERIRUN_ZOHO_CLIENT_SECRET": settings.zoho_client_secret.get_secret_value(),
        "VERIRUN_ZOHO_TOKEN_KEY": settings.zoho_token_key.get_secret_value(),
    }
    read_refresh = settings.zoho_read_refresh_token.get_secret_value()
    write_refresh = settings.zoho_write_refresh_token.get_secret_value()
    requested = (
        any(configured.values())
        or bool(read_refresh)
        or bool(write_refresh)
        or settings.zoho_read_token_file.is_file()
        or settings.zoho_write_token_file.is_file()
    )
    if not requested:
        return None
    missing = [name for name, value in configured.items() if not value]
    if missing:
        raise ValueError(
            "Zoho Books is partially configured; set " + ", ".join(missing)
        )
    if not read_refresh and not settings.zoho_read_token_file.is_file():
        raise ValueError(
            "Zoho Books needs VERIRUN_ZOHO_READ_REFRESH_TOKEN or an existing "
            f"token file at {settings.zoho_read_token_file}"
        )
    if not write_refresh and not settings.zoho_write_token_file.is_file():
        raise ValueError(
            "Zoho Books needs VERIRUN_ZOHO_WRITE_REFRESH_TOKEN or an existing "
            f"token file at {settings.zoho_write_token_file}"
        )
    read_client = _zoho_client(
        settings,
        refresh_token=read_refresh,
        token_file=settings.zoho_read_token_file,
        read_only=True,
    )
    write_client = _zoho_client(
        settings,
        refresh_token=write_refresh,
        token_file=settings.zoho_write_token_file,
        read_only=False,
    )
    read_only = ZohoAccountingReads(read_client)
    write_reads = ZohoAccountingReads(write_client)
    return read_only, ZohoAccountingSystem(write_reads)


def _zoho_client(
    settings: Settings,
    *,
    refresh_token: str,
    token_file: Path,
    read_only: bool,
) -> ZohoBooksClient:
    oauth = ZohoOAuthConfig(
        client_id=settings.zoho_client_id,
        client_secret=settings.zoho_client_secret.get_secret_value(),
        refresh_token=refresh_token,
        accounts_domain=settings.zoho_accounts_domain,
        timeout=settings.zoho_timeout_s,
        read_only=read_only,
    )
    store = ZohoTokenStore(token_file, settings.zoho_token_key.get_secret_value())
    return ZohoBooksClient(
        token_manager=ZohoTokenManager(oauth, store),
        organization_id=settings.zoho_organization_id,
        base_url=settings.zoho_api_base_url,
        timeout=settings.zoho_timeout_s,
    )


def sync_mail(backends: Backends) -> int:
    """Ingest new mail before a new Run; a resume reads its already-ingested source.

    The CLI calls this before starting a Run. The dashboard only resumes parked
    Runs, so it deliberately does not sync: a mailbox error must not block an
    approval decision.
    """
    return backends.mail.sync()


def run_policy_facts(
    store: RunStore, run_id: str, task_pack: TaskPack
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Facts the engine already recorded that approval rules need.

    A bill's approval can depend on the extraction that produced it, not only
    on the bill arguments. Reading the Run's own observations here keeps that
    fact out of the model's hands: the model cannot claim a confidence it did
    not produce.
    """
    threshold = (
        task_pack.extraction.confidence_threshold if task_pack.extraction else None
    )

    def facts(args: dict[str, Any]) -> dict[str, Any]:
        number = args.get("number")
        extractions = [
            record
            for record in store.list_observations(run_id)
            if record.tool == INVOICE_EXTRACT_TOOL
        ]
        chosen = None
        if isinstance(number, str) and number:
            for record in reversed(extractions):
                parsed = record.data.get("parsed") or {}
                if parsed.get("invoice_number") == number:
                    chosen = record
                    break
        if chosen is None:
            successes = [record for record in extractions if record.ok]
            chosen = successes[-1] if successes else None
        result: dict[str, Any] = {
            "purchase_order_required": task_pack.requires_purchase_order,
        }
        if chosen is not None:
            data = chosen.data or {}
            minimum = data.get("min_confidence")
            result["extraction_min_confidence"] = minimum
            result["extraction_below_threshold"] = bool(
                data.get("low_confidence")
                or (
                    isinstance(minimum, (int, float))
                    and threshold is not None
                    and minimum < threshold
                )
            )
        return result

    return facts


def build_registry(
    settings: Settings,
    context: CompanyContext,
    task_pack: TaskPack,
    client: LLMClient | None = None,
    *,
    backends: Backends,
    browser: BrowserSession | None = None,
    approval_gates: bool = True,
    policy_facts: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
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
        *build_erp_tools(
            backends.accounting, default_currency=task_pack.default_currency
        ),
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
    if task_pack.extraction is not None and INVOICE_EXTRACT_TOOL in task_pack.tools:
        if client is None:
            raise ValueError(
                f"Task Pack '{task_pack.id}' reads India GST invoices; "
                "build_registry needs an LLM client"
            )
        tools.append(
            ExtractIndiaInvoiceTool(
                root,
                client,
                confidence_threshold=task_pack.extraction.confidence_threshold,
                prices=settings.prices,
            )
        )
    return ToolRegistry.from_task_pack(
        tools,
        task_pack,
        gate=PolicyGate(
            context.policies,
            approval_gates=approval_gates,
            run_facts=policy_facts,
            policy_ids=task_pack.policies,
        ),
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
    facts = run_policy_facts(RunStore(settings.run_db), run_id, task_pack)
    try:
        yield build_registry(
            settings,
            context,
            task_pack,
            client,
            backends=backends,
            browser=session,
            policy_facts=facts,
        )
    finally:
        session.close()
