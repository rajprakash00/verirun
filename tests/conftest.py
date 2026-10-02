from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mocks.ledgerlite.app import create_app
from mocks.seed.ledgerlite import reset_and_seed


@pytest.fixture
def ledgerlite_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "ledgerlite.db"
    reset_and_seed(db_path)
    return db_path


@pytest.fixture
def client(ledgerlite_db: Path) -> Iterator[TestClient]:
    with TestClient(create_app(ledgerlite_db)) as test_client:
        yield test_client
