"""Tests for choosing mail and accounting backends from settings.

The Mock Suite stays the default. Setting an IMAP host or a watched folder
swaps only the mail source; accounting stays the deterministic fake until the
Zoho adapter lands.
"""

from __future__ import annotations

from pathlib import Path

from verirun.adapters import ImapSource, MockMailSource, WatchedFolderSource
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
