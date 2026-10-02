from pathlib import Path

from scripts.seed_mocks import main


def test_seed_mocks_script_seeds_the_whole_suite(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "state"
    shared = tmp_path / "shared"

    assert main(["--state-dir", str(state_dir), "--shared", str(shared)]) == 0

    assert "Mock Suite" in capsys.readouterr().out
    assert (state_dir / "ledgerlite.db").is_file()
    assert (state_dir / "maildesk.db").is_file()
    for name in ("documents", "archive", "processed"):
        assert (shared / name).is_dir(), name
    assert len(list((shared / "documents" / "invoices").glob("*.pdf"))) == 8
