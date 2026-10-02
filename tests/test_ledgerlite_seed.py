import sqlite3
from contextlib import closing
from pathlib import Path

from mocks.ledgerlite.db import TABLES
from mocks.seed.ledgerlite import reset_and_seed


def dump(db_path: Path) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            table: [
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in TABLES
        }


def test_seed_is_reproducible_across_databases(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"

    reset_and_seed(first)
    reset_and_seed(second)

    assert dump(first) == dump(second)
    assert all(rows for rows in dump(first).values())


def test_reseeding_the_same_database_restores_ground_truth(tmp_path: Path) -> None:
    db = tmp_path / "ledgerlite.db"

    reset_and_seed(db)
    ground_truth = dump(db)
    reset_and_seed(db)

    assert dump(db) == ground_truth


def test_seed_covers_every_invoice_scenario(tmp_path: Path) -> None:
    db = tmp_path / "ledgerlite.db"
    reset_and_seed(db)

    with closing(sqlite3.connect(db)) as conn:
        scenarios = {
            row[0] for row in conn.execute("SELECT scenario FROM vendors")
        } | {row[0] for row in conn.execute("SELECT scenario FROM invoices")}

    assert {
        "happy",
        "duplicate",
        "amount_mismatch",
        "missing_po",
        "over_limit",
        "forbidden_action",
        "transient",
        "scanned",
    } <= scenarios
