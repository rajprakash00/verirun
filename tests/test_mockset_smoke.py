import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from mocks.maildesk.app import create_app
from mocks.seed.suite import reset_and_seed_suite


def test_seed_suite_serves_the_inbox_and_the_shared_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    shared_root = tmp_path / "shared"

    reset_and_seed_suite(
        state_dir / "ledgerlite.db",
        state_dir / "maildesk.db",
        shared_root,
    )

    with closing(sqlite3.connect(state_dir / "ledgerlite.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 9

    for name in ("documents", "archive", "processed"):
        assert (shared_root / name).is_dir(), name
    assert len(list((shared_root / "documents" / "invoices").glob("*.pdf"))) == 8

    with TestClient(create_app(state_dir / "maildesk.db", shared_root)) as client:
        inbox = client.get("/")
        assert inbox.status_code == 200
        assert "Invoice batch for processing" in inbox.text

        messages = client.get("/api/messages").json()
        assert len(messages) == 11
        for message in messages:
            detail = client.get(f"/api/messages/{message['id']}").json()
            for attachment in detail["attachments"]:
                response = client.get(f"/attachments/{attachment['id']}/download")
                assert response.status_code == 200
                assert response.content == (shared_root / attachment["path"]).read_bytes()
