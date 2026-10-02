import sqlite3
from contextlib import closing
from pathlib import Path

from scripts.seed_ledgerlite import main


def test_seed_script_writes_ground_truth(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ledgerlite.db"

    assert main(["--db", str(db_path)]) == 0

    assert "Seeded" in capsys.readouterr().out
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2
