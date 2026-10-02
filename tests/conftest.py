from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient

from mocks.ledgerlite.app import create_app
from mocks.maildesk.app import create_app as create_maildesk_app
from mocks.seed.ledgerlite import reset_and_seed
from mocks.seed.maildesk import reset_and_seed as reset_and_seed_maildesk


class MaildeskState(NamedTuple):
    db_path: Path
    shared_root: Path


@pytest.fixture
def ledgerlite_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "ledgerlite.db"
    reset_and_seed(db_path)
    return db_path


@pytest.fixture
def client(ledgerlite_db: Path) -> Iterator[TestClient]:
    with TestClient(create_app(ledgerlite_db)) as test_client:
        yield test_client


@pytest.fixture
def maildesk_state(tmp_path: Path) -> MaildeskState:
    state = MaildeskState(tmp_path / "maildesk.db", tmp_path / "shared")
    reset_and_seed_maildesk(state.db_path, state.shared_root)
    return state


@pytest.fixture
def maildesk_client(maildesk_state: MaildeskState) -> Iterator[TestClient]:
    with TestClient(
        create_maildesk_app(maildesk_state.db_path, maildesk_state.shared_root)
    ) as test_client:
        yield test_client
