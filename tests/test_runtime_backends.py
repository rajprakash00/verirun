"""Tests for choosing mail and accounting backends from settings.

The Mock Suite stays the default. Setting an IMAP host or a watched folder
swaps only the mail source; accounting stays the deterministic fake until the
Zoho adapter lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verirun.adapters import ImapSource, MockMailSource, WatchedFolderSource
from verirun.adapters.zoho import (
    ZohoAccountingReads,
    ZohoAccountingSystem,
)
from verirun.config import Settings
from verirun.runtime import Backends


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        mail_db=tmp_path / "maildesk.db",
        erp_db=tmp_path / "ledgerlite.db",
        shared_dir=tmp_path / "shared",
        mail_state_db=tmp_path / "mail-index.db",
        **overrides,
    )


def zoho_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "zoho_organization_id": "6000000000001",
        "zoho_client_id": "client-id",
        "zoho_client_secret": "client-secret",
        "zoho_read_refresh_token": "refresh-token",
        "zoho_token_key": "operator-secret",
        "zoho_read_token_file": tmp_path / "zoho-read-token.enc",
    }
    values.update(overrides)
    return settings(tmp_path, **values)


def test_the_mock_suite_is_the_default_mail_source(tmp_path: Path) -> None:
    backends = Backends.from_settings(settings(tmp_path))

    assert isinstance(backends.mail, MockMailSource)


def test_a_watched_folder_swaps_the_mail_source(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    chosen = settings(tmp_path, watch_dir=watch)

    backends = Backends.from_settings(chosen)

    assert isinstance(backends.mail, WatchedFolderSource)
    assert backends.mail.watch_dir == watch
    assert backends.mail.document_root == tmp_path / "shared"


def test_an_imap_host_swaps_the_mail_source(tmp_path: Path) -> None:
    chosen = settings(
        tmp_path,
        imap_host="imap.example.test",
        imap_port=1993,
        imap_username="ap@example.test",
        imap_password="app-password",
        imap_folder="AP/Invoices",
    )

    backends = Backends.from_settings(chosen)

    assert isinstance(backends.mail, ImapSource)
    assert backends.mail.host == "imap.example.test"
    assert backends.mail.port == 1993
    assert backends.mail.username == "ap@example.test"
    assert backends.mail.folder == "AP/Invoices"
    assert backends.mail.document_root == tmp_path / "shared"
    assert "app-password" not in repr(chosen)


def test_imap_is_preferred_when_both_sources_are_configured(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()

    backends = Backends.from_settings(
        settings(
            tmp_path,
            imap_host="imap.example.test",
            imap_username="ap@example.test",
            imap_password="app-password",
            watch_dir=watch,
        )
    )

    assert isinstance(backends.mail, ImapSource)


def test_zoho_reads_replace_the_mock_accounting_when_configured(
    tmp_path: Path,
) -> None:
    backends = Backends.from_settings(zoho_settings(tmp_path))

    assert isinstance(backends.reads, ZohoAccountingReads)
    assert isinstance(backends.accounting, ZohoAccountingSystem)
    assert backends.verifier_accounting is backends.reads
    assert backends.verifier_accounting is not backends.accounting
    assert "client-secret" not in repr(zoho_settings(tmp_path))


def test_a_token_file_makes_the_environment_refresh_token_optional(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "zoho-read-token.enc"
    token_file.write_bytes(b"encrypted-token")
    chosen = zoho_settings(
        tmp_path,
        zoho_read_refresh_token="",
        zoho_read_token_file=token_file,
    )

    backends = Backends.from_settings(chosen)

    assert isinstance(backends.reads, ZohoAccountingReads)


def test_a_partial_zoho_configuration_is_rejected(tmp_path: Path) -> None:
    chosen = settings(
        tmp_path,
        zoho_organization_id="6000000000001",
    )

    with pytest.raises(ValueError, match="Zoho Books is partially configured"):
        Backends.from_settings(chosen)


def test_zoho_without_a_token_or_file_is_rejected(tmp_path: Path) -> None:
    chosen = zoho_settings(tmp_path, zoho_read_refresh_token="")

    with pytest.raises(ValueError, match="REFRESH_TOKEN"):
        Backends.from_settings(chosen)
